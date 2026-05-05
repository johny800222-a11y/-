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

    period_re = re.compile(r"期別[：:]?\s*(\d{9,})")
    num_re    = re.compile(r"\b(\d{2})\b")
    time_re   = re.compile(r"\((\d{2}:\d{2})\)")
    date_re   = re.compile(r"(\d{4}/\d{1,2}/\d{1,2})")

    records = []
    i = 0
    cur_date = datetime.today().strftime("%Y/%m/%d")

    while i < len(lines):
        dm = date_re.search(lines[i])
        if dm:
            cur_date = dm.group(1)
            i += 1
            continue

        pm = period_re.search(lines[i])
        if pm:
            period = pm.group(1)
            blob = " ".join(lines[i:i+4])
            nums = num_re.findall(blob)
            nums = [int(n) for n in nums if 1 <= int(n) <= 80]
            nums = list(dict.fromkeys(nums))[:20]  # dedupe, keep order

            tm = time_re.search(blob)
            draw_time = tm.group(1) if tm else ""

            if len(nums) == 20:
                row = {"period": period, "date": cur_date, "time": draw_time}
                row.update({f"n{j+1}": nums[j] for j in range(20)})
                records.append(row)
            i += 1
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
        return existing, False

    new_df = pd.DataFrame(new_recs)
    new_df["datetime"] = pd.to_datetime(new_df["date"] + " " + new_df["time"], errors="coerce")

    if existing is None or existing.empty:
        df = new_df.drop_duplicates("period").sort_values("datetime").reset_index(drop=True)
    else:
        df = pd.concat([existing, new_df]).drop_duplicates("period").sort_values("datetime").reset_index(drop=True)

    updated = existing is None or len(df) > len(existing)
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
