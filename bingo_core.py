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


def smart_pick(df: pd.DataFrame, window: int = 30) -> dict:
    """
    回傳智慧選號結果：
    - hot_top10: 熱號 Top 10（含頻次）
    - top_pairs: 共現最強的 4 對
    - six: 6星推薦（6個號碼）
    - nine: 9星推薦（9個號碼 = 6星 + 3個擴展）
    - scores: 6星/9星的熱度分/關聯分
    """
    hot = hot_numbers(df, window)
    comat = cooccurrence_matrix(df, window)

    # 熱號 Top 10
    hot_sorted = sorted(hot, key=lambda x: x["cnt"], reverse=True)
    hot_top10 = hot_sorted[:10]

    # 各號碼的關聯強度（與所有其他號碼的共現總次數）
    pair_strength: dict[int, int] = Counter()
    for (a, b), cnt in comat.items():
        pair_strength[a] += cnt
        pair_strength[b] += cnt

    # 綜合分數 = 頻次 * 2 + 關聯強度
    scores = {
        n["num"]: n["cnt"] * 2 + pair_strength.get(n["num"], 0)
        for n in hot
    }

    # 6星：綜合分前 6
    six = sorted(BALL_RANGE, key=lambda n: scores[n], reverse=True)[:6]
    six_set = set(six)

    # 擴展候選：與六星共現最強的號碼（排除已選）
    ext_scores: dict[int, int] = Counter()
    for n in six:
        for (a, b), cnt in comat.items():
            partner = b if a == n else (a if b == n else None)
            if partner and partner not in six_set:
                ext_scores[partner] += cnt

    ext3 = sorted(ext_scores, key=lambda n: ext_scores[n], reverse=True)[:3]
    nine = six + ext3

    # 評分指標
    hot_avg = sum(x["cnt"] for x in hot) / len(BALL_RANGE)
    hot_max = max(x["cnt"] for x in hot) or 1

    def heat_score(nums):
        return round(sum(hot[n-1]["cnt"] for n in nums) / (len(nums) * hot_max) * 100)

    def pair_score(nums):
        pairs = [(a, b) for i, a in enumerate(nums) for b in nums[i+1:] if a < b]
        s = sum(comat.get((a, b), 0) for a, b in pairs)
        mx = max(comat.values()) if comat else 1
        return round(s / (len(pairs) * mx) * 100) if pairs else 0

    # 共現 Top 4 配對
    top_pairs = []
    for (a, b), cnt in sorted(comat.items(), key=lambda x: x[1], reverse=True)[:4]:
        max_cnt = max(comat.values()) if comat else 1
        top_pairs.append({
            "a": a, "b": b, "cnt": cnt,
            "pct": round(cnt / max_cnt * 100)
        })

    return {
        "hot_top10": hot_top10,
        "top_pairs": top_pairs,
        "six":       sorted(six),
        "nine":      sorted(nine),
        "scores": {
            "six_heat":  heat_score(six),
            "six_pair":  pair_score(six),
            "nine_heat": heat_score(nine),
            "nine_pair": pair_score(nine),
        }
    }
