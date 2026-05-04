"""
今彩539 核心邏輯（資料抓取 + 推薦演算法）
推薦權重 = 歷史頻率 × 學習權重（每期對獎後動態更新）
"""

import re
import time
import random
from pathlib import Path
from datetime import datetime
from collections import Counter

import requests
from bs4 import BeautifulSoup
import pandas as pd

DATA_FILE   = Path(__file__).parent / "539_history.csv"
BASE_URL    = "https://www.pilio.idv.tw/lto539/list539BIG.asp"
TOTAL_PAGES = 59
SLEEP_SEC   = 0.4
HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; lottery539-app/1.0)"}
BALL_COLS   = ["n1", "n2", "n3", "n4", "n5"]
BALL_RANGE  = list(range(1, 40))


# ── 抓取 ──────────────────────────────────────────────────────────────────────

def _fetch_page(page: int) -> list[dict]:
    url  = f"{BASE_URL}?indexpage={page}&orderby=old"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.encoding = "big5"
    text = BeautifulSoup(resp.text, "html.parser").get_text("\n")

    records  = []
    date_re  = re.compile(r"(\d{4}/\d{2}/\d{2})\([^)]+\)")
    num_re   = re.compile(r"(\d{2}),\s*(\d{2}),\s*(\d{2}),\s*(\d{2}),\s*(\d{2})")
    lines    = [l.strip() for l in text.splitlines() if l.strip()]

    i = 0
    while i < len(lines):
        dm = date_re.search(lines[i])
        if dm:
            blob = lines[i] + (" " + lines[i + 1] if i + 1 < len(lines) else "")
            nm   = num_re.search(blob)
            if nm:
                records.append({
                    "date": dm.group(1),
                    "n1": int(nm.group(1)), "n2": int(nm.group(2)),
                    "n3": int(nm.group(3)), "n4": int(nm.group(4)),
                    "n5": int(nm.group(5)),
                })
                i += 2
                continue
        i += 1
    return records


def fetch_all(progress_cb=None) -> pd.DataFrame:
    all_records = []
    for page in range(1, TOTAL_PAGES + 1):
        try:
            all_records.extend(_fetch_page(page))
            if progress_cb:
                progress_cb(page, TOTAL_PAGES)
            time.sleep(SLEEP_SEC)
        except Exception:
            pass

    df = pd.DataFrame(all_records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df.to_csv(DATA_FILE, index=False)
    return df


def update_latest() -> tuple[pd.DataFrame, bool]:
    df = pd.read_csv(DATA_FILE, parse_dates=["date"])
    new_records = _fetch_page(TOTAL_PAGES)
    new_df = pd.DataFrame(new_records)
    new_df["date"] = pd.to_datetime(new_df["date"])
    combined = pd.concat([df, new_df]).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    updated = len(combined) > len(df)
    combined.to_csv(DATA_FILE, index=False)
    return combined, updated


def load_data() -> pd.DataFrame | None:
    if not DATA_FILE.exists():
        return None
    return pd.read_csv(DATA_FILE, parse_dates=["date"])


# ── 推薦 ──────────────────────────────────────────────────────────────────────

def _ok(nums: list[int]) -> bool:
    if len(set(nums)) != 5:
        return False
    odds = sum(1 for n in nums if n % 2 != 0)
    return odds in (2, 3) and 75 <= sum(nums) <= 125


def recommend_probability(df: pd.DataFrame, learn_weights: dict[int, float] | None = None) -> list[int]:
    """
    頻率加權 × 學習權重 → 熱門且近期準確的號碼優先
    """
    freq    = Counter(df[BALL_COLS].values.flatten().tolist())
    pool    = sorted(freq)
    # 合併頻率權重與學習權重
    weights = [
        freq[n] * (learn_weights.get(n, 1.0) if learn_weights else 1.0)
        for n in pool
    ]
    for _ in range(30000):
        chosen = sorted(set(random.choices(pool, weights=weights, k=10)))[:5]
        if len(chosen) == 5 and _ok(chosen):
            return chosen
    return sorted(random.sample(BALL_RANGE, 5))


def recommend_value(df: pd.DataFrame, learn_weights: dict[int, float] | None = None) -> list[int]:
    """
    避開人群 + 近期冷門 + 學習權重修正
    """
    hot50 = set(n for n, _ in Counter(
        df.tail(50)[BALL_COLS].values.flatten().tolist()
    ).most_common(10))

    high  = list(range(32, 40))
    low   = [n for n in range(1, 32) if n not in hot50] or list(range(1, 32))

    # 學習權重：選擇近期「被低估但實際開出」的號碼
    if learn_weights:
        low  = sorted(low,  key=lambda n: learn_weights.get(n, 1.0), reverse=True)[:20]
        high = sorted(high, key=lambda n: learn_weights.get(n, 1.0), reverse=True)

    for _ in range(30000):
        try:
            cand = sorted(set(random.sample(high, 2) + random.sample(low, 3)))
        except ValueError:
            continue
        if len(cand) != 5:
            continue
        if any(cand[i+1] - cand[i] == 1 for i in range(4)):
            continue
        if _ok(cand):
            return cand
    return sorted(random.sample(BALL_RANGE, 5))


# ── 統計 ──────────────────────────────────────────────────────────────────────

def get_stats(df: pd.DataFrame) -> dict:
    freq    = Counter(df[BALL_COLS].values.flatten().tolist())
    hot     = freq.most_common(10)
    cold    = sorted(BALL_RANGE, key=lambda n: freq.get(n, 0))[:10]
    latest  = df.iloc[-1]
    recent  = df.tail(10)

    return {
        "total":        len(df),
        "date_start":   df["date"].min().strftime("%Y-%m-%d"),
        "date_end":     df["date"].max().strftime("%Y-%m-%d"),
        "latest_date":  latest["date"].strftime("%Y-%m-%d"),
        "latest_nums":  [int(latest[c]) for c in BALL_COLS],
        "hot_nums":     [{"num": n, "count": c} for n, c in hot],
        "cold_nums":    [{"num": n, "count": freq.get(n, 0)} for n in cold],
        "recent_draws": [
            {
                "date": row["date"].strftime("%Y-%m-%d"),
                "nums": [int(row[c]) for c in BALL_COLS],
            }
            for _, row in recent.iloc[::-1].iterrows()
        ],
    }
