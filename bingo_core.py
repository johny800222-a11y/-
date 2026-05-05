"""
Bingo Bingo 核心邏輯：資料抓取 + 模型分析 + 推薦
每5分鐘一期，從1-80開出20個號碼
"""

import re
import time
from pathlib import Path
from datetime import datetime
from collections import Counter

import requests
from bs4 import BeautifulSoup
import pandas as pd

DATA_FILE  = Path(__file__).parent / "bingo_history.csv"
BASE_URL   = "https://www.pilio.idv.tw/bingo/list.asp"
HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; lottery539-app/1.0)"}
NUM_COLS   = [f"n{i}" for i in range(1, 21)]
BALL_RANGE = list(range(1, 81))


# ── 抓取 ───────────────────────────────────────────────────────────────────

def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    period_re = re.compile(r"(\d{9,})")
    time_re   = re.compile(r"\((\d{2}:\d{2})\)")
    date_re   = re.compile(r"(\d{4}/\d{1,2}/\d{1,2})")

    records = []
    i = 0
    cur_date = datetime.today().strftime("%Y/%m/%d")

    while i < len(lines):
        dm = date_re.search(lines[i])
        if dm and "BINGO" not in lines[i] and len(lines[i]) < 30:
            cur_date = dm.group(1)
            i += 1
            continue

        pm = period_re.search(lines[i])
        if pm and len(pm.group(1)) >= 9:
            period = pm.group(1)
            # 往後收集號碼：單獨數字行（1-80），遇到時間行停止
            nums = []
            j = i + 1
            draw_time = ""
            while j < len(lines) and len(nums) < 20:
                line = lines[j]
                tm = time_re.search(line)
                if tm:
                    draw_time = tm.group(1)
                    break
                if re.fullmatch(r"\d{1,2}", line):
                    n = int(line)
                    if 1 <= n <= 80:
                        nums.append(n)
                j += 1

            if len(nums) == 20:
                row = {"period": period, "date": cur_date, "time": draw_time}
                row.update({f"n{k+1}": nums[k] for k in range(20)})
                records.append(row)
            i = j + 1
            continue
        i += 1

    return records


def fetch_recent(pages: int = 3) -> pd.DataFrame:
    """抓最近N頁資料（每頁約50期）"""
    all_records = []
    for page in range(1, pages + 1):
        try:
            url  = f"{BASE_URL}?indexpage={page}&orderby=old"
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.encoding = "big5"
            all_records.extend(_parse_page(resp.text))
            time.sleep(0.4)
        except Exception:
            pass

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"], errors="coerce")
    df = df.drop_duplicates("period").sort_values("datetime").reset_index(drop=True)
    return df


def update_latest() -> tuple[pd.DataFrame, bool]:
    existing = load_data()
    new_recs  = _parse_page(_get_html(BASE_URL))
    if not new_recs:
        if existing is None:
            return pd.DataFrame(), False
        return existing, False

    new_df = pd.DataFrame(new_recs)
    new_df["datetime"] = pd.to_datetime(new_df["date"] + " " + new_df["time"], errors="coerce")

    if existing is None or existing.empty:
        df = new_df.drop_duplicates("period").sort_values("datetime").reset_index(drop=True)
        updated = True
    else:
        df = pd.concat([existing, new_df]).drop_duplicates("period").sort_values("datetime").reset_index(drop=True)
        updated = len(df) > len(existing)

    df.to_csv(DATA_FILE, index=False)
    return df, updated


def _get_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.encoding = "big5"
    return resp.text


def load_data() -> pd.DataFrame | None:
    if not DATA_FILE.exists():
        return None
    df = pd.read_csv(DATA_FILE)
    if df.empty:
        return None
    return df


def init_data() -> pd.DataFrame:
    """首次初始化：抓最近3頁（約150期）"""
    df = fetch_recent(pages=3)
    if not df.empty:
        df.to_csv(DATA_FILE, index=False)
    return df


# ── 模型分類 ────────────────────────────────────────────────────────────────

def classify(nums: list[int]) -> dict[str, bool]:
    """對一期的20個號碼做模型分類（可多重命中）"""
    s = sorted(nums)
    odd_count  = sum(1 for n in s if n % 2 != 0)
    even_count = 20 - odd_count
    low_count  = sum(1 for n in s if n <= 40)
    high_count = 20 - low_count

    # 連號組數
    consec = sum(1 for i in range(len(s)-1) if s[i+1] - s[i] == 1)

    # 重複（需比較前幾期，這裡先回傳特徵供外部計算）
    return {
        "A": odd_count >= 12,
        "B": even_count >= 12,
        "C": consec >= 3,
        "E": low_count >= 13,
        "F": high_count >= 13,
        "_odd": odd_count,
        "_even": even_count,
        "_low": low_count,
        "_high": high_count,
        "_consec": consec,
    }


def classify_df(df: pd.DataFrame) -> pd.DataFrame:
    """對整份資料做模型分類，含 D（號碼重複）"""
    rows = []
    nums_list = df[NUM_COLS].values.tolist()

    for i, nums in enumerate(nums_list):
        c = classify(nums)
        # D：與前5期重疊數量 >= 8
        if i >= 5:
            prev = [n for prev_nums in nums_list[i-5:i] for n in prev_nums]
            prev_count = Counter(prev)
            overlap = sum(1 for n in nums if prev_count.get(n, 0) > 0)
            c["D"] = overlap >= 8
        else:
            c["D"] = False
        rows.append(c)

    flags = pd.DataFrame(rows)
    return pd.concat([df.reset_index(drop=True), flags], axis=1)


# ── 模型機率計算 ────────────────────────────────────────────────────────────

MODEL_KEYS = ["A", "B", "C", "D", "E", "F"]
MODEL_NAMES = {
    "A": "奇數偏重", "B": "偶數偏重", "C": "連號密集",
    "D": "號碼重複", "E": "低區偏重", "F": "高區偏重",
}


def model_probs(df: pd.DataFrame, recent_n: int = 100) -> dict[str, float]:
    """
    用最近 recent_n 期計算各模型機率，近期期數加權（越近越重）。
    回傳各模型機率（加總可能 > 1，因模型可重疊）。
    """
    classified = classify_df(df.tail(recent_n).copy())
    n = len(classified)
    if n == 0:
        return {k: 1/6 for k in MODEL_KEYS}

    # 線性加權：最舊的 weight=1，最新的 weight=n
    weights = list(range(1, n + 1))
    total_w = sum(weights)

    probs = {}
    for k in MODEL_KEYS:
        if k not in classified.columns:
            probs[k] = 0.0
            continue
        w_sum = sum(w for w, v in zip(weights, classified[k]) if v)
        probs[k] = w_sum / total_w

    # 正規化為百分比（讓加總=100%，方便顯示）
    total = sum(probs.values()) or 1
    return {k: round(v / total * 100, 1) for k, v in probs.items()}


# ── 號碼機率熱力圖 ──────────────────────────────────────────────────────────

def num_probs(df: pd.DataFrame, probs: dict[str, float], recent_n: int = 100) -> list[dict]:
    """
    對 1-80 每個號碼計算加權出現機率。
    各模型對號碼的傾向加成，乘以模型機率後加總。
    """
    classified = classify_df(df.tail(recent_n).copy())
    n = len(classified)
    weights = list(range(1, n + 1))
    total_w = sum(weights)

    # 各模型下每個號碼的加權出現次數
    model_num_freq: dict[str, Counter] = {k: Counter() for k in MODEL_KEYS}

    nums_list = classified[NUM_COLS].values.tolist()
    for i, (row_nums, w) in enumerate(zip(nums_list, weights)):
        for k in MODEL_KEYS:
            if classified.iloc[i].get(k, False):
                for n_ in row_nums:
                    model_num_freq[k][int(n_)] += w

    # 各模型正規化後乘以模型機率加總
    result = {n: 0.0 for n in BALL_RANGE}
    for k in MODEL_KEYS:
        freq = model_num_freq[k]
        s = sum(freq.values()) or 1
        model_w = probs.get(k, 0) / 100
        for n_ in BALL_RANGE:
            result[n_] += (freq.get(n_, 0) / s) * model_w

    # 正規化為百分比
    total = sum(result.values()) or 1
    return [{"num": n_, "pct": round(result[n_] / total * 100, 2)} for n_ in BALL_RANGE]


# ── 10星推薦 ────────────────────────────────────────────────────────────────

def top10(num_prob_list: list[dict]) -> list[dict]:
    return sorted(num_prob_list, key=lambda x: x["pct"], reverse=True)[:10]


# ── 猜大小 / 猜單雙 ─────────────────────────────────────────────────────────

def guess_bigsmall(num_prob_list: list[dict]) -> dict:
    big   = sum(x["pct"] for x in num_prob_list if x["num"] > 40)
    small = sum(x["pct"] for x in num_prob_list if x["num"] <= 40)
    total = big + small or 1
    big_pct   = round(big / total * 100)
    small_pct = 100 - big_pct
    winner = "大" if big_pct >= small_pct else "小"
    conf   = max(big_pct, small_pct)
    return {"answer": winner, "conf": conf, "big": big_pct, "small": small_pct}


def guess_oddeven(num_prob_list: list[dict]) -> dict:
    odd  = sum(x["pct"] for x in num_prob_list if x["num"] % 2 != 0)
    even = sum(x["pct"] for x in num_prob_list if x["num"] % 2 == 0)
    total = odd + even or 1
    odd_pct  = round(odd / total * 100)
    even_pct = 100 - odd_pct
    winner = "單" if odd_pct >= even_pct else "雙"
    conf   = max(odd_pct, even_pct)
    return {"answer": winner, "conf": conf, "odd": odd_pct, "even": even_pct}


# ── 24小時勝率 ──────────────────────────────────────────────────────────────

def winrate_24h(df: pd.DataFrame) -> dict:
    """
    對近24小時（約288期）逐期回測：
    - 用前N期資料預測第N+1期的猜大小/猜單雙
    - 對比實際超級獎號（以各期20個號碼的中位數模擬）
    - 回傳整體/猜大小/猜單雙 勝率
    """
    # 取近288期（24小時）+ 前100期做預測用
    window = 288
    lookback = 100
    total_needed = window + lookback

    if len(df) < lookback + 2:
        return {"overall": 0, "bigsmall": 0, "oddeven": 0,
                "total": 0, "bs_hits": 0, "oe_hits": 0, "yesterday": None}

    recent = df.tail(total_needed).reset_index(drop=True)
    eval_start = max(lookback, len(recent) - window)

    bs_hits = oe_hits = total = 0

    for i in range(eval_start, len(recent)):
        history = recent.iloc[:i]
        if len(history) < 10:
            continue

        p = model_probs(history, recent_n=min(100, len(history)))
        np_list = num_probs(history, p, recent_n=min(100, len(history)))

        bs_pred = guess_bigsmall(np_list)["answer"]
        oe_pred = guess_oddeven(np_list)["answer"]

        # 實際結果：用當期20個號碼的中位數判斷大小/單雙
        actual_nums = [int(recent.iloc[i][c]) for c in NUM_COLS]
        median_num  = sorted(actual_nums)[9]  # 第10小的號碼作為代表
        actual_bs   = "大" if median_num > 40 else "小"
        actual_oe   = "單" if median_num % 2 != 0 else "雙"

        if bs_pred == actual_bs:
            bs_hits += 1
        if oe_pred == actual_oe:
            oe_hits += 1
        total += 1

    if total == 0:
        return {"overall": 0, "bigsmall": 0, "oddeven": 0,
                "total": 0, "bs_hits": 0, "oe_hits": 0}

    bs_rate = round(bs_hits / total * 100)
    oe_rate = round(oe_hits / total * 100)
    overall = round((bs_hits + oe_hits) / (total * 2) * 100)

    # 前一天勝率（用前window期做對比，若資料不足則None）
    yesterday = None
    if len(df) >= total_needed + window:
        prev_df = df.iloc[-(total_needed + window):-window]
        prev_wr = winrate_24h(prev_df)
        yesterday = {
            "overall":   prev_wr["overall"],
            "bigsmall":  prev_wr["bigsmall"],
            "oddeven":   prev_wr["oddeven"],
        }

    return {
        "overall":   overall,
        "bigsmall":  bs_rate,
        "oddeven":   oe_rate,
        "total":     total,
        "bs_hits":   bs_hits,
        "oe_hits":   oe_hits,
        "yesterday": yesterday,
    }
