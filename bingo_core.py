"""
Bingo Bingo 核心邏輯：資料抓取 + 模型分析 + 推薦
每5分鐘一期，從1-80開出20個號碼
"""

import re
import time
import pytz
from pathlib import Path
from datetime import datetime
from collections import Counter

_TW = pytz.timezone("Asia/Taipei")

def _tw_today() -> str:
    now = datetime.now(_TW)
    return f"{now.year}/{now.month}/{now.day}"  # 無補零，符合網站格式 2026/5/6

import requests
from bs4 import BeautifulSoup
import pandas as pd

_DATA_DIR    = Path("/data") if Path("/data").exists() else Path(__file__).parent
DATA_FILE    = _DATA_DIR / "bingo_history.csv"
BASE_URL     = "https://www.pilio.idv.tw/bingo/list.asp"
HISTORY_URL  = "https://www.pilio.idv.tw/bingo/list_history.asp"
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; lottery539-app/1.0)"}
NUM_COLS     = [f"n{i}" for i in range(1, 21)]
BALL_RANGE   = list(range(1, 81))


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
    cur_date = _tw_today()

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


def _parse_history_page(html: str, date_str: str) -> list[dict]:
    """解析 list_history.asp 的格式（逐期一行，逗號分隔號碼）"""
    period_re = re.compile(r"(\d{9,})")
    time_re   = re.compile(r"\((\d{2}:\d{2})\)")
    num_re    = re.compile(r"\b([1-9]|[1-7]\d|80)\b")

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    records = []

    for row in rows:
        text = row.get_text(" ")
        pm = period_re.search(text)
        if not pm or len(pm.group(1)) < 9:
            continue
        period = pm.group(1)

        tm = time_re.search(text)
        draw_time = tm.group(1) if tm else ""

        # 號碼在期號之後，用逗號分隔的 1-80 整數
        after = text[pm.end():]
        seen = set()
        nums = []
        for m in num_re.finditer(after):
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                nums.append(n)
            if len(nums) == 20:
                break

        if len(nums) == 20:
            row_data = {"period": period, "date": date_str, "time": draw_time}
            row_data.update({f"n{k+1}": nums[k] for k in range(20)})
            records.append(row_data)

    return records


def _available_dates(html: str) -> list[str]:
    """從 list.asp 或 list_history.asp 取出可選的日期清單"""
    return re.findall(r'<option value="(\d{4}/\d+/\d+)"', html)


def fetch_by_dates(dates: list[str]) -> pd.DataFrame:
    """按日期清單從 list_history.asp 逐日抓取"""
    all_records = []
    for d in dates:
        try:
            resp = requests.get(HISTORY_URL, params={"indate": d},
                                headers=HEADERS, timeout=20)
            resp.encoding = "big5"
            recs = _parse_history_page(resp.text, d)
            all_records.extend(recs)
            time.sleep(0.6)
        except Exception:
            pass

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"], errors="coerce")
    df = df.drop_duplicates("period").sort_values("datetime").reset_index(drop=True)
    return df


def fetch_recent(pages: int = 3) -> pd.DataFrame:
    """抓最近N頁資料（每頁約50期，用於向下相容）"""
    all_records = []
    for page in range(1, pages + 1):
        try:
            url  = f"{BASE_URL}?indexpage={page}"
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.encoding = "big5"
            all_records.extend(_parse_page(resp.text))
            time.sleep(0.8)
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
    # 若資料太少（冷啟動），改用多頁補抓
    if existing is None or len(existing) < 50:
        df = init_data()
        return df, not df.empty

    new_recs  = _parse_page(_get_html(BASE_URL))
    if not new_recs:
        return existing, False

    new_df = pd.DataFrame(new_recs)
    new_df["datetime"] = pd.to_datetime(new_df["date"] + " " + new_df["time"], errors="coerce")

    if existing is None or existing.empty:
        df = new_df.drop_duplicates("period").sort_values("datetime").reset_index(drop=True)
        updated = True
    else:
        existing["datetime"] = pd.to_datetime(existing.get("datetime"), errors="coerce")
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
    """
    首次初始化：從 list_history.asp 按日期抓最近30天，
    確保有足夠樣本做回測，再補抓今日最新期數。
    """
    try:
        index_html = _get_html(BASE_URL)
        dates = _available_dates(index_html)
    except Exception:
        dates = []

    df = fetch_by_dates(dates) if dates else pd.DataFrame()

    # 若逐日抓失敗，fallback 到舊方式
    if df.empty:
        df = fetch_recent(pages=8)

    # 補抓最新一頁（確保今日最新期）
    if not df.empty:
        try:
            new_recs = _parse_page(index_html)
            if new_recs:
                new_df = pd.DataFrame(new_recs)
                new_df["datetime"] = pd.to_datetime(new_df["date"] + " " + new_df["time"], errors="coerce")
                df = pd.concat([df, new_df]).drop_duplicates("period").sort_values("datetime").reset_index(drop=True)
        except Exception:
            pass
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


# ── 當日勝率 ──────────────────────────────────────────────────────────────

def winrate_recent(df: pd.DataFrame, window: int = 30) -> dict:
    """
    回測今日最新 window 期（預設30期）的猜大小/猜單雙勝率。
    換日後自動重置，歷史資料作為預測依據。
    - yesterday: 昨日同樣 window 期的勝率對比
    """
    lookback = 100

    today_str = _tw_today()
    today_df  = df[df["date"] == today_str] if "date" in df.columns else pd.DataFrame()
    eval_window = today_df.tail(window) if not today_df.empty else pd.DataFrame()

    if eval_window.empty or len(df) < lookback + 2:
        return {"overall": 0, "bigsmall": 0, "oddeven": 0,
                "total": 0, "bs_hits": 0, "oe_hits": 0, "yesterday": None}

    # 歷史資料（今日之前的所有資料，作為 lookback）
    history_base = df[df["date"] != today_str].tail(lookback)

    bs_hits = oe_hits = total = 0

    eval_rows = eval_window.reset_index(drop=True)
    eval_start = 0

    for i in range(len(eval_rows)):
        # 預測用歷史：昨日前 lookback 期 + 今日前 i 期
        today_so_far = eval_rows.iloc[:i]
        history = pd.concat([history_base, today_so_far]).reset_index(drop=True)
        if len(history) < 10:
            continue

        p = model_probs(history, recent_n=min(100, len(history)))
        np_list = num_probs(history, p, recent_n=min(100, len(history)))

        bs_pred = guess_bigsmall(np_list)["answer"]
        oe_pred = guess_oddeven(np_list)["answer"]

        actual_nums = [int(eval_rows.iloc[i][c]) for c in NUM_COLS]
        median_num  = sorted(actual_nums)[9]
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

    # 昨日勝率對比
    yesterday = None
    import datetime as _dt
    yest = datetime.now(_TW).date() - _dt.timedelta(days=1)
    yesterday_str = f"{yest.year}/{yest.month}/{yest.day}"
    yest_df = df[df["date"] == yesterday_str] if "date" in df.columns else pd.DataFrame()
    if not yest_df.empty:
        # 昨日歷史 = 昨日之前的 lookback
        yest_history_base = df[df["date"] < yesterday_str].tail(lookback)
        yest_eval = yest_df.tail(window).reset_index(drop=True)
        ybs = yoe = ytotal = 0
        for i in range(len(yest_eval)):
            h = pd.concat([yest_history_base, yest_eval.iloc[:i]]).reset_index(drop=True)
            if len(h) < 10:
                continue
            p2 = model_probs(h, recent_n=min(100, len(h)))
            np2 = num_probs(h, p2, recent_n=min(100, len(h)))
            bp = guess_bigsmall(np2)["answer"]
            op = guess_oddeven(np2)["answer"]
            an = [int(yest_eval.iloc[i][c]) for c in NUM_COLS]
            mn = sorted(an)[9]
            if bp == ("大" if mn > 40 else "小"): ybs += 1
            if op == ("單" if mn % 2 != 0 else "雙"): yoe += 1
            ytotal += 1
        if ytotal:
            yesterday = {
                "overall":  round((ybs + yoe) / (ytotal * 2) * 100),
                "bigsmall": round(ybs / ytotal * 100),
                "oddeven":  round(yoe / ytotal * 100),
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


# 向下相容舊名稱
def winrate_24h(df: pd.DataFrame) -> dict:
    return winrate_recent(df, window=30)


# ── 三同數：三號共現組合 ─────────────────────────────────────────────────────

def triplet_cooccurrence(df: pd.DataFrame, window: int = 200, top_n: int = 20) -> list[dict]:
    """
    統計最近 window 期，三個號碼同時出現在同一期的次數（三同數）。
    只回傳前 top_n 組，計算量大，window 不宜過大。
    """
    recent = df.tail(window)
    triplet_cnt: Counter = Counter()

    for _, row in recent.iterrows():
        nums = sorted(int(row[c]) for c in NUM_COLS)
        # 只統計相鄰號碼的三連組（避免 C(20,3)=1140 組全算）
        # 改為：取所有三組中，至少有一對差值 ≤ 10 的組合（熱門連帶模式）
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                if nums[j] - nums[i] > 30:
                    continue  # 距離太遠跳過
                for k in range(j + 1, len(nums)):
                    if nums[k] - nums[i] > 30:
                        break
                    triplet_cnt[(nums[i], nums[j], nums[k])] += 1

    return [
        {"a": a, "b": b, "c": c, "cnt": cnt}
        for (a, b, c), cnt in triplet_cnt.most_common(top_n)
    ]


def consecutive_pair_freq(df: pd.DataFrame, window: int = 200) -> dict[tuple, int]:
    """統計連號對（差值=1）的共現頻率"""
    recent = df.tail(window)
    consec: Counter = Counter()
    for _, row in recent.iterrows():
        nums = sorted(int(row[c]) for c in NUM_COLS)
        for i in range(len(nums) - 1):
            if nums[i + 1] - nums[i] == 1:
                consec[(nums[i], nums[i + 1])] += 1
    return dict(consec)


# ── 二同數分析 ──────────────────────────────────────────────────────────────

def same_tail_analysis(df: pd.DataFrame, window: int = 100) -> dict:
    """
    分析最近 window 期的二同數（同尾數號碼）出現規律。
    回傳：
      hot_tails: 各尾數（0-9）產生二同的頻率，由高到低排序
      avg_pairs: 每期平均二同組數
      tail_pair_nums: 各尾數最熱的號碼（出現最多次）
    """
    recent = df.tail(window)
    tail_pair_count = Counter()  # 各尾數產生二同的次數
    tail_num_freq   = {t: Counter() for t in range(10)}  # 各尾數下各號碼頻次
    pair_per_draw   = []

    for _, row in recent.iterrows():
        nums = [int(row[c]) for c in NUM_COLS]
        tail_cnt = Counter(n % 10 for n in nums)
        pairs = 0
        for t, c in tail_cnt.items():
            tail_num_freq[t].update(n for n in nums if n % 10 == t)
            if c >= 2:
                tail_pair_count[t] += 1
                pairs += 1
        pair_per_draw.append(pairs)

    avg_pairs = sum(pair_per_draw) / len(pair_per_draw) if pair_per_draw else 6.5

    hot_tails = sorted(range(10), key=lambda t: tail_pair_count[t], reverse=True)

    # 各尾數下最熱的兩個號碼
    tail_top2 = {}
    for t in range(10):
        top = [n for n, _ in tail_num_freq[t].most_common(2)]
        tail_top2[t] = top

    return {
        "hot_tails":     hot_tails,          # 尾數由熱到冷
        "tail_freq":     dict(tail_pair_count),
        "avg_pairs":     round(avg_pairs, 1),
        "tail_top2":     tail_top2,          # 各尾數前2熱號
    }


# ── 智慧選號 ────────────────────────────────────────────────────────────────

def hot_numbers(df: pd.DataFrame, window: int = 30) -> list[dict]:
    """最近 window 期各號碼出現頻次，並標記是否高於平均。"""
    recent = df.tail(window)
    freq: Counter = Counter()
    for _, row in recent.iterrows():
        for col in NUM_COLS:
            freq[int(row[col])] += 1

    avg = sum(freq.values()) / len(BALL_RANGE)
    return [
        {"num": n, "cnt": freq.get(n, 0), "hot": freq.get(n, 0) > avg}
        for n in BALL_RANGE
    ]


def cooccurrence_matrix(df: pd.DataFrame, window: int = 30) -> dict[tuple[int,int], int]:
    """最近 window 期任意兩號碼共現次數。"""
    recent = df.tail(window)
    comat: Counter = Counter()
    for _, row in recent.iterrows():
        nums = sorted(int(row[c]) for c in NUM_COLS)
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                comat[(nums[i], nums[j])] += 1
    return dict(comat)


def smart_pick(df: pd.DataFrame, window: int = 30, learn_weights: dict = None) -> dict:
    """
    多策略投票選號系統（v3）：
    ══════════════════════════════════════════════════════
    6 個獨立策略各自提名候選號碼，票數最多者勝出。
    策略投票權重由 bingo_learner 每週根據命中率自動調整。

    策略清單：
      S1 超短期動能（最近3期出現≥2次的球 × 1.5倍追漲）
      S2 短期熱號（近20期頻率最高前12名）
      S3 冷號回歸（遺漏超過期望值的球，按遺漏程度排名）
      S4 強力共現（近100期共現次數最高的球）
      S5 區間峰值（1-20/21-40/41-60/61-80 各取最熱前3）
      S6 學習模型（learner weights × pair_weights 綜合評分）

    最終選號：票數最多 → 分數最高（同票時加成處理）
    6星/9星完全獨立選號，不互相牽制
    ══════════════════════════════════════════════════════
    """
    import bingo_learner as _bl

    if learn_weights is None:
        learn_weights = _bl.get_weights()

    total_rows = len(df)
    ball_cols  = [c for c in df.columns if c.startswith("n") and c[1:].isdigit()]

    # ══════════════════════════════════════════════════════════════
    # 窗口設計（以天數換算期數，上限 90 天）
    # Bingo 每天 ~288 期（每5分鐘一期）
    # ══════════════════════════════════════════════════════════════
    DRAWS_PER_DAY = 288

    def days(d):
        return min(d * DRAWS_PER_DAY, total_rows)

    wU  = min(5,         total_rows)   # 超短期：最近5期（~25分鐘）
    wS  = days(0.2)                    # 短期：最近~4小時（~50期）
    w1d = days(1)                      # 1天 (~288期)
    w7d = days(7)                      # 7天 (~2016期)
    w30d= days(30)                     # 30天 (~8640期)
    w90d= days(90)                     # 90天上限 (~25920期)

    def freq_counter(rows):
        c = Counter()
        for _, row in rows.iterrows():
            for col in NUM_COLS:
                c[int(row[col])] += 1
        return c

    freqU  = freq_counter(df.tail(wU))
    freqS  = freq_counter(df.tail(wS))
    freq1d = freq_counter(df.tail(w1d))
    freq7d = freq_counter(df.tail(w7d))
    freq30d= freq_counter(df.tail(w30d))

    def exp(w):
        return max(w * 20 / 80, 0.001)

    expU   = exp(wU)
    expS   = exp(wS)
    exp1d  = exp(w1d)
    exp7d  = exp(w7d)
    exp30d = exp(w30d)

    # 遺漏期數（以90天資料計算）
    recent90 = df.tail(w90d)
    last_seen = {}
    for i, row_nums in enumerate(recent90[NUM_COLS].values.tolist()):
        for n in row_nums:
            last_seen[int(n)] = i
    n90 = len(recent90)
    expected_gap = 80 / 20   # 每球平均 4 期一次

    def miss_periods(n):
        return n90 - 1 - last_seen.get(n, 0)

    # 共現矩陣（近7天，窗口夠大才有統計意義）
    comat = cooccurrence_matrix(df, w7d)
    sorted_pairs = sorted(comat.items(), key=lambda x: x[1], reverse=True)
    max_comat = max(comat.values()) if comat else 1

    # 每個號碼的共現強度
    pair_strength: dict[int, int] = Counter()
    for (a, b), cnt in comat.items():
        pair_strength[a] += cnt
        pair_strength[b] += cnt

    # 數對迭代學習
    pair_learn_w = _bl.get_pair_weights()
    pair_learn_bonus: dict[int, float] = {}
    for n in BALL_RANGE:
        related = [pair_learn_w[(min(n, m), max(n, m))]
                   for m in BALL_RANGE if m != n and (min(n, m), max(n, m)) in pair_learn_w]
        pair_learn_bonus[n] = sum(related) / len(related) if related else 1.0
    avg_pb = sum(pair_learn_bonus.values()) / len(pair_learn_bonus)
    if avg_pb > 0:
        pair_learn_bonus = {n: v / avg_pb for n, v in pair_learn_bonus.items()}

    # 從 bingo_learner 取策略權重（每週自動更新）
    strategy_weights = _bl.get_strategy_weights()
    # 預設各策略同等權重 1.0，週深度學習後會自動調整
    sw = {
        "s1_momentum":  strategy_weights.get("s1_momentum",  1.0),
        "s2_hot":       strategy_weights.get("s2_hot",       1.0),
        "s3_cold":      strategy_weights.get("s3_cold",      1.0),
        "s4_cooccur":   strategy_weights.get("s4_cooccur",   1.0),
        "s5_zone":      strategy_weights.get("s5_zone",      1.0),
        "s6_learner":   strategy_weights.get("s6_learner",   1.0),
    }

    # ══════════════════════════════════════════════════════════════
    # 6 個獨立策略各自提名候選（提名前 N 名）
    # ══════════════════════════════════════════════════════════════
    NOM6 = 14   # 6星：每策略提名14名，從中取票數最多的6顆
    NOM9 = 18   # 9星：每策略提名18名，從中取票數最多的9顆

    def top_n_from(scores_dict, n):
        return sorted(BALL_RANGE, key=lambda x: scores_dict.get(x, 0), reverse=True)[:n]

    # ── 熱度方向偵測（動能 vs 衰退）─────────────────────────────────
    # 比較超短期(5期)頻率 vs 短期(4小時)基線，判斷各球是在升溫還是降溫
    # trajectory > 1.2 = 升溫（動能球）; < 0.8 = 降溫（衰退球）
    def heat_trajectory(n):
        rate_now  = freqU.get(n, 0) / expU     # 最近5期出現率
        rate_base = freqS.get(n, 0) / expS     # 4小時基線
        if rate_base < 0.01:
            return 1.0
        t = rate_now / rate_base
        # 限制倍率：最多 ×1.8（升溫）/ 最低 ×0.6（強衰退）
        return max(0.6, min(1.8, t))

    # S1：超短期動能 — 最近5期 + 4小時頻率（追當前熱球）+ 熱度方向加成
    s1_scores = {}
    for n in BALL_RANGE:
        rU = freqU.get(n, 0) / expU
        rS = freqS.get(n, 0) / expS
        base = rU * 0.65 + rS * 0.35
        s1_scores[n] = base * heat_trajectory(n)   # 升溫球加分，衰退球減分
    s1_nom6 = top_n_from(s1_scores, NOM6)
    s1_nom9 = top_n_from(s1_scores, NOM9)

    # S2：短期熱號 — 近1天 + 近7天頻率（抓日週趨勢）
    s2_scores = {}
    for n in BALL_RANGE:
        r1d = freq1d.get(n, 0) / exp1d
        r7d = freq7d.get(n, 0) / exp7d
        s2_scores[n] = r1d * 0.60 + r7d * 0.40
    s2_nom6 = top_n_from(s2_scores, NOM6)
    s2_nom9 = top_n_from(s2_scores, NOM9)

    # S3：冷號回歸 — 遺漏期數超過期望值越多，分數越高（以90天為基準）
    s3_scores = {}
    for n in BALL_RANGE:
        miss  = miss_periods(n)
        ratio = miss / expected_gap
        s3_scores[n] = min(ratio, 8.0)
    s3_nom6 = top_n_from(s3_scores, NOM6)
    s3_nom9 = top_n_from(s3_scores, NOM9)

    # S4：強力共現 — 近7天共現強度最高（窗口夠大有統計意義）
    s4_scores = {}
    max_ps = max(pair_strength.values()) if pair_strength else 1
    for n in BALL_RANGE:
        s4_scores[n] = pair_strength.get(n, 0) / max_ps
    s4_nom6 = top_n_from(s4_scores, NOM6)
    s4_nom9 = top_n_from(s4_scores, NOM9)

    # S5：區間峰值 — 每個區間(1-20/21-40/41-60/61-80)取最熱
    # 用近30天頻率找各區間頂尖球（月度視角）
    zones = [range(1, 21), range(21, 41), range(41, 61), range(61, 81)]
    s5_nom6, s5_nom9 = [], []
    per_zone6 = max(1, NOM6 // 4)
    per_zone9 = max(1, NOM9 // 4)
    for z in zones:
        zone_ranked = sorted(z, key=lambda n: freq30d.get(n, 0), reverse=True)
        s5_nom6.extend(zone_ranked[:per_zone6])
        s5_nom9.extend(zone_ranked[:per_zone9])
    all_by_freq30d = sorted(BALL_RANGE, key=lambda n: freq30d.get(n, 0), reverse=True)
    for n in all_by_freq30d:
        if len(s5_nom6) >= NOM6:
            break
        if n not in s5_nom6:
            s5_nom6.append(n)
    for n in all_by_freq30d:
        if len(s5_nom9) >= NOM9:
            break
        if n not in s5_nom9:
            s5_nom9.append(n)

    # S6：學習模型 — learner weights × pair_learn_bonus
    s6_scores = {}
    for n in BALL_RANGE:
        s6_scores[n] = learn_weights.get(n, 1.0) * pair_learn_bonus.get(n, 1.0)
    s6_nom6 = top_n_from(s6_scores, NOM6)
    s6_nom9 = top_n_from(s6_scores, NOM9)

    # ══════════════════════════════════════════════════════════════
    # 投票整合：加權票數 + 綜合分數作 tiebreaker
    # ══════════════════════════════════════════════════════════════
    def vote_and_pick(nom6_lists, nom9_lists, weights_dict, target):
        """
        nom6_lists: [(strategy_key, nomination_list), ...]
        weights_dict: {strategy_key: weight}
        target: 6 or 9
        """
        nom_lists = nom6_lists if target == 6 else nom9_lists
        votes = Counter()
        for key, nominees in nom_lists:
            w = weights_dict.get(key, 1.0)
            for n in nominees:
                votes[n] += w

        # tiebreaker：綜合分數（所有策略分數加權均值）
        combo_score = {}
        for n in BALL_RANGE:
            combo_score[n] = (
                s1_scores.get(n, 0) * sw["s1_momentum"] +
                s2_scores.get(n, 0) * sw["s2_hot"] +
                s3_scores.get(n, 0) / 8 * sw["s3_cold"] +
                s4_scores.get(n, 0) * sw["s4_cooccur"] +
                freq1d.get(n, 0) / exp1d * sw["s5_zone"] +
                s6_scores.get(n, 0) * sw["s6_learner"]
            )

        # 依 (票數, 綜合分) 排序，取前 target 名
        all_ranked = sorted(BALL_RANGE,
                            key=lambda n: (votes.get(n, 0), combo_score.get(n, 0)),
                            reverse=True)
        return all_ranked[:target], votes, combo_score

    nom_pairs_6 = [
        ("s1_momentum", s1_nom6),
        ("s2_hot",      s2_nom6),
        ("s3_cold",     s3_nom6),
        ("s4_cooccur",  s4_nom6),
        ("s5_zone",     s5_nom6),
        ("s6_learner",  s6_nom6),
    ]
    nom_pairs_9 = [
        ("s1_momentum", s1_nom9),
        ("s2_hot",      s2_nom9),
        ("s3_cold",     s3_nom9),
        ("s4_cooccur",  s4_nom9),
        ("s5_zone",     s5_nom9),
        ("s6_learner",  s6_nom9),
    ]

    six_raw,  votes6, combo6 = vote_and_pick(nom_pairs_6, nom_pairs_9, sw, 6)
    nine_raw, votes9, combo9 = vote_and_pick(nom_pairs_6, nom_pairs_9, sw, 9)

    # ── 區間分散強制：避免全部集中在同一個20號區間 ─────────────────
    # 80個球分4區（1-20/21-40/41-60/61-80），6星最多允許同區3球
    def diversify(picks, target_n, max_same_zone=3):
        """
        若某區球數超過 max_same_zone，
        用該區外分數最高的球替換掉該區分數最低的球。
        """
        result = list(picks)
        for attempt in range(10):
            zone_cnt = {0: [], 1: [], 2: [], 3: []}
            for n in result:
                zone_cnt[(n - 1) // 20].append(n)

            overcrowded = {z: ns for z, ns in zone_cnt.items() if len(ns) > max_same_zone}
            if not overcrowded:
                break

            for z, ns in overcrowded.items():
                # 要替換掉的：此區分數最低的球
                weakest = min(ns, key=lambda n: combo6.get(n, 0))
                result.remove(weakest)
                # 補入：不在 result 裡、不在過滿區、分數最高的
                excluded = set(result)
                for n in sorted(BALL_RANGE, key=lambda x: votes6.get(x, 0) + combo6.get(x, 0) * 0.01, reverse=True):
                    if n not in excluded and (n - 1) // 20 != z:
                        result.append(n)
                        break
        return result[:target_n]

    six  = diversify(six_raw,  6, max_same_zone=3)
    nine = diversify(nine_raw, 9, max_same_zone=4)

    # ══════════════════════════════════════════════════════════════
    # 供前端顯示的附加資訊
    # ══════════════════════════════════════════════════════════════
    hot_short = hot_numbers(df, w1d)   # 近1天為熱號基準
    hot_top10 = sorted(hot_short, key=lambda x: x["cnt"], reverse=True)[:10]
    hot_max   = max((x["cnt"] for x in hot_short), default=1)

    top_pairs_display = [
        {"a": a, "b": b, "cnt": cnt, "pct": round(cnt / max_comat * 100)}
        for (a, b), cnt in sorted_pairs[:4]
    ]
    triplets       = triplet_cooccurrence(df, window=w7d, top_n=5)
    consec_freq_d  = consecutive_pair_freq(df, window=w7d)
    top_consec     = [
        {"a": a, "b": b, "cnt": cnt}
        for (a, b), cnt in sorted(consec_freq_d.items(), key=lambda x: x[1], reverse=True)[:5]
    ]

    def heat_score(nums):
        freq_map = {x["num"]: x["cnt"] for x in hot_short}
        return round(sum(freq_map.get(n, 0) for n in nums) / (len(nums) * hot_max) * 100)

    def pair_score(nums):
        s_pairs = [(a, b) for i, a in enumerate(nums) for b in nums[i+1:] if a < b]
        s = sum(comat.get((a, b), 0) for a, b in s_pairs)
        return round(s / (len(s_pairs) * max_comat) * 100) if s_pairs else 0

    def avg_hits_backtest(nums, lookback=50):
        recent  = df.tail(lookback)
        num_set = set(nums)
        total_hits = total = 0
        for _, row in recent.iterrows():
            drawn = set(int(row[c]) for c in ball_cols if row[c] > 0)
            if drawn:
                total += 1
                total_hits += len(num_set & drawn)
        return round(total_hits / total, 2) if total else 0.0

    def backtest_pct(nums, min_hits, lookback=50):
        recent  = df.tail(lookback)
        num_set = set(nums)
        hits = total = 0
        for _, row in recent.iterrows():
            drawn = set(int(row[c]) for c in ball_cols if row[c] > 0)
            if drawn:
                total += 1
                if len(num_set & drawn) >= min_hits:
                    hits += 1
        return round(hits / total * 100) if total else 0

    def find_strong_pairs_in(nums):
        num_set = set(nums)
        return [{"a": a, "b": b, "cnt": cnt, "pct": round(cnt / max_comat * 100)}
                for (a, b), cnt in sorted_pairs[:20]
                if a in num_set and b in num_set]

    # 策略提名分布（供學習器追蹤）
    strategy_nominations = {
        "s1": {"six": s1_nom6[:6], "nine": s1_nom9[:9]},
        "s2": {"six": s2_nom6[:6], "nine": s2_nom9[:9]},
        "s3": {"six": s3_nom6[:6], "nine": s3_nom9[:9]},
        "s4": {"six": s4_nom6[:6], "nine": s4_nom9[:9]},
        "s5": {"six": s5_nom6[:6], "nine": s5_nom9[:9]},
        "s6": {"six": s6_nom6[:6], "nine": s6_nom9[:9]},
    }

    return {
        "hot_top10":      hot_top10,
        "top_pairs":      top_pairs_display,
        "top_triplets":   triplets,
        "top_consec":     top_consec,
        "six":            sorted(six),
        "nine":           sorted(nine),
        "six_pairs":      find_strong_pairs_in(six),
        "nine_pairs":     find_strong_pairs_in(nine),
        "strategy_nominations": strategy_nominations,
        "strategy_weights":     sw,
        "scores": {
            "six_heat":   heat_score(six),
            "six_pair":   pair_score(six),
            "six_prob":   backtest_pct(six,  min_hits=2),
            "six_avg":    avg_hits_backtest(six),
            "nine_heat":  heat_score(nine),
            "nine_pair":  pair_score(nine),
            "nine_prob":  backtest_pct(nine, min_hits=3),
            "nine_avg":   avg_hits_backtest(nine),
        }
    }
