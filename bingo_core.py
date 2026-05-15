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
    多層訊號智慧選號：
      短期熱號（30期）× 中期頻率（200期）× 長期基準（全量）
      冷號補正（超過 N 期未出現 → 補分）
      統計偏差（chi-square 方向加成）
      學習權重（每日迭代更新）
      區間平衡（1-20 / 21-40 / 41-60 / 61-80 各至少1個）
    """
    if learn_weights is None:
        import bingo_learner
        learn_weights = bingo_learner.get_weights()

    total_rows = len(df)

    # ── 三層頻率統計 ────────────────────────────────────────────────
    def freq_counter(rows):
        c = Counter()
        for _, row in rows.iterrows():
            for col in NUM_COLS:
                c[int(row[col])] += 1
        return c

    short_w   = min(30,  total_rows)
    mid_w     = min(200, total_rows)
    long_w    = total_rows

    freq_s = freq_counter(df.tail(short_w))   # 短期
    freq_m = freq_counter(df.tail(mid_w))      # 中期
    freq_l = freq_counter(df)                   # 長期

    # 各層期望值（均勻分布下每號出現次數）
    exp_s = short_w * 20 / 80
    exp_m = mid_w  * 20 / 80
    exp_l = long_w * 20 / 80

    # ── 冷號補正：超過 cold_threshold 期沒出現的號碼加分 ────────────
    cold_threshold = 25  # 超過25期未出現
    last_seen = {}
    nums_list = df[NUM_COLS].values.tolist()
    for i, nums in enumerate(nums_list):
        for n in nums:
            last_seen[int(n)] = i
    def cold_bonus(n):
        last = last_seen.get(n, 0)
        gap = total_rows - 1 - last
        if gap >= cold_threshold:
            return 1.0 + min(1.5, (gap - cold_threshold) / cold_threshold)
        return 1.0

    # ── 共現強度（中期視窗） ──────────────────────────────────────
    comat = cooccurrence_matrix(df, mid_w)
    pair_strength: dict[int, int] = Counter()
    for (a, b), cnt in comat.items():
        pair_strength[a] += cnt
        pair_strength[b] += cnt
    max_pair = max(pair_strength.values()) if pair_strength else 1

    # ── 綜合分數 ────────────────────────────────────────────────────
    # 三層頻率歸一化後加權混合
    # 短期（近期趨勢）×0.5 + 中期（統計穩定）×0.35 + 長期（基準修正）×0.15
    # 再乘以：冷號補正 × 學習權重 × 關聯強度加成
    scores = {}
    for n in BALL_RANGE:
        s_score = freq_s.get(n, 0) / exp_s      # 短期相對頻率
        m_score = freq_m.get(n, 0) / exp_m      # 中期
        l_score = freq_l.get(n, 0) / exp_l      # 長期

        combined = s_score * 0.50 + m_score * 0.35 + l_score * 0.15
        combined *= cold_bonus(n)
        combined *= learn_weights.get(n, 1.0)
        # 關聯強度加成（上限 1.3 倍）
        combined *= min(1.3, 1.0 + 0.3 * pair_strength.get(n, 0) / max_pair)

        scores[n] = max(combined, 0.001)

    # ── 熱號 Top 10（短期視窗，供前端顯示） ───────────────────────
    hot_short = hot_numbers(df, window)
    hot_top10 = sorted(hot_short, key=lambda x: x["cnt"], reverse=True)[:10]

    # ── 區間平衡選號 ────────────────────────────────────────────────
    zones = [range(1, 21), range(21, 41), range(41, 61), range(61, 81)]

    def best_from_zone(z, exclude, n_pick):
        candidates = sorted(
            [num for num in z if num not in exclude],
            key=lambda num: scores.get(num, 0), reverse=True
        )
        return candidates[:n_pick]

    all_ranked = sorted(BALL_RANGE, key=lambda n: scores.get(n, 0), reverse=True)

    # ── 二同數分析：民間核心玩法 ─────────────────────────────────
    # 每期平均有 6~7 組二同，選號必須包含至少 1 對同尾號碼
    sta = same_tail_analysis(df, window=min(200, total_rows))
    hot_tails = sta["hot_tails"]   # 尾數由熱到冷
    tail_top2 = sta["tail_top2"]   # 各尾數前2熱號

    def pick_same_tail_pair(tail, exclude):
        """從指定尾數中選出得分最高的2個號碼（二同對）"""
        candidates = sorted(
            [n for n in BALL_RANGE if n % 10 == tail and n not in exclude],
            key=lambda n: scores.get(n, 0), reverse=True
        )
        return candidates[:2]

    # 6星：先從最熱尾數取一對二同（2個），再區間平衡補4個
    six = []
    # 步驟1：最熱尾數二同對
    for t in hot_tails:
        pair = pick_same_tail_pair(t, set(six))
        if len(pair) == 2:
            six.extend(pair)
            break
    # 步驟2：區間平衡補足（確保4個區間各有覆蓋）
    for z in zones:
        if len(six) >= 6:
            break
        picked = best_from_zone(z, set(six), 1)
        six.extend(picked)
    # 步驟3：若仍不足，從全域高分補
    for n in all_ranked:
        if len(six) >= 6:
            break
        if n not in six:
            six.append(n)

    # 9星：在6星基礎上再加第二對二同 + 區間補足至9個
    nine = list(six)
    # 加第二對二同（從次熱尾數選）
    for t in hot_tails:
        if len(nine) >= 9:
            break
        # 跳過已被6星使用的尾數對
        existing_tails = [n % 10 for n in nine]
        if existing_tails.count(t) >= 2:
            continue
        pair = pick_same_tail_pair(t, set(nine))
        if len(pair) >= 1:
            nine.extend(pair[:min(2, 9 - len(nine))])
            if len(nine) >= 9:
                break
    for z in zones:
        if len(nine) >= 9:
            break
        nine.extend(best_from_zone(z, set(nine), 1))
    for n in all_ranked:
        if len(nine) >= 9:
            break
        if n not in nine:
            nine.append(n)

    # ── 評分指標 ───────────────────────────────────────────────────
    hot_max = max(x["cnt"] for x in hot_short) or 1

    def heat_score(nums):
        freq_map = {x["num"]: x["cnt"] for x in hot_short}
        return round(sum(freq_map.get(n, 0) for n in nums) / (len(nums) * hot_max) * 100)

    def pair_score(nums):
        pairs = [(a, b) for i, a in enumerate(nums) for b in nums[i+1:] if a < b]
        s = sum(comat.get((a, b), 0) for a, b in pairs)
        mx = max(comat.values()) if comat else 1
        return round(s / (len(pairs) * mx) * 100) if pairs else 0

    top_pairs = []
    for (a, b), cnt in sorted(comat.items(), key=lambda x: x[1], reverse=True)[:4]:
        max_cnt = max(comat.values()) if comat else 1
        top_pairs.append({"a": a, "b": b, "cnt": cnt,
                          "pct": round(cnt / max_cnt * 100)})

    # 統計選出的二同對
    def count_same_tail_pairs(nums):
        tc = Counter(n % 10 for n in nums)
        return [(t, [n for n in nums if n % 10 == t]) for t, c in tc.items() if c >= 2]

    return {
        "hot_top10":       hot_top10,
        "top_pairs":       top_pairs,
        "six":             sorted(six),
        "nine":            sorted(nine),
        "six_same_tail":   count_same_tail_pairs(six),   # 6星中的二同對
        "nine_same_tail":  count_same_tail_pairs(nine),  # 9星中的二同對
        "hot_tails":       hot_tails[:5],                # 前5熱尾數
        "avg_pairs_per_draw": sta["avg_pairs"],
        "scores": {
            "six_heat":  heat_score(six),
            "six_pair":  pair_score(six),
            "nine_heat": heat_score(nine),
            "nine_pair": pair_score(nine),
        }
    }
