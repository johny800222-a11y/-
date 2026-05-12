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

_DATA_DIR   = Path("/data") if Path("/data").exists() else Path(__file__).parent
DATA_FILE   = _DATA_DIR / "539_history.csv"
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
    if not (odds in (2, 3) and 75 <= sum(nums) <= 125):
        return False
    # 不允許超過2個連號（3個以上連號視為異常）
    s = sorted(nums)
    consec = sum(1 for i in range(len(s) - 1) if s[i + 1] - s[i] == 1)
    return consec <= 2


def recommend_best(df: pd.DataFrame, learn_weights: dict[int, float] | None = None) -> list[int]:
    """
    大數據綜合推薦：
      歷史頻率（長期）× 學習權重（近期命中修正）× 遺漏值修正（近50期未出現補正）
    三層訊號加乘後做加權抽樣，兼顧熱門趨勢與冷號補回，
    並通過奇偶比 2:3/3:2、總和 75-125 的合理性過濾。
    """
    freq    = Counter(df[BALL_COLS].values.flatten().tolist())
    total   = sum(freq.values())
    avg_freq = total / len(BALL_RANGE)

    # 近50期遺漏修正：久未出現的號碼給予補正加成
    recent50 = Counter(df.tail(50)[BALL_COLS].values.flatten().tolist())
    avg_recent = sum(recent50.values()) / len(BALL_RANGE)

    pool    = sorted(BALL_RANGE)
    weights = []
    for n in pool:
        w = freq.get(n, 0) / avg_freq                          # 長期頻率
        w *= (learn_weights.get(n, 1.0) if learn_weights else 1.0)  # 學習修正
        # 近50期遺漏越多 → 補正越大（最高 1.5 倍）
        miss_bonus = 1.0 + 0.5 * max(0, avg_recent - recent50.get(n, 0)) / avg_recent
        w *= miss_bonus
        # 連號傾向加成：相鄰號碼在近50期出現越多，本號權重越高（上限1.3倍）
        adj = recent50.get(n - 1, 0) + recent50.get(n + 1, 0)
        w *= min(1.3, 1.0 + 0.15 * adj / max(avg_recent, 1))
        weights.append(max(w, 0.01))

    for _ in range(50000):
        chosen = sorted(set(random.choices(pool, weights=weights, k=10)))[:5]
        if len(chosen) == 5 and _ok(chosen):
            return chosen
    return sorted(random.sample(BALL_RANGE, 5))


# ── 策略推薦 ──────────────────────────────────────────────────────────────────

def strategy_recommend(df: pd.DataFrame) -> dict:
    """
    三策略合併選號：
      策略1 冷熱號碼：近10期熱號2碼 + 超過25期未出現冷號1碼
      策略2 尾數群聚：取近30期最熱門尾數，補入1碼同尾號
      策略3 分散區間：低(1-13)/中(14-26)/高(27-39) 各區保證至少1碼
    最終5碼通過奇偶比/總和過濾，並附帶各策略分析說明。
    """
    recent10  = Counter(df.tail(10)[BALL_COLS].values.flatten().tolist())
    recent30  = Counter(df.tail(30)[BALL_COLS].values.flatten().tolist())

    # ── 策略1：冷熱號碼 ──────────────────────────────────────────
    # 熱號：近10期出現次數最多
    hot_nums  = [n for n, _ in sorted(recent10.items(), key=lambda x: x[1], reverse=True)]
    # 冷號：超過25期未出現
    all_periods = df[BALL_COLS].values.tolist()
    last_seen   = {}
    for i, row in enumerate(all_periods):
        for n in row:
            last_seen[int(n)] = i
    total_rows = len(all_periods)
    cold_nums = sorted(
        [n for n in BALL_RANGE if total_rows - 1 - last_seen.get(n, 0) >= 25],
        key=lambda n: total_rows - 1 - last_seen.get(n, 0), reverse=True
    )

    # ── 策略2：尾數群聚 ──────────────────────────────────────────
    tail_freq = Counter(n % 10 for n in recent30.elements())
    hot_tail  = tail_freq.most_common(2)  # 最熱兩個尾數
    tail_candidates = []
    for tail, _ in hot_tail:
        tail_candidates += [n for n in BALL_RANGE if n % 10 == tail]

    # ── 策略3：分散區間 ──────────────────────────────────────────
    zones = {"低": list(range(1, 14)), "中": list(range(14, 27)), "高": list(range(27, 40))}

    def best_in_zone(zone_nums, exclude):
        candidates = [n for n in zone_nums if n not in exclude]
        if not candidates:
            return None
        # 優先熱號，其次尾數候選，最後一般
        for n in hot_nums:
            if n in candidates:
                return n
        for n in tail_candidates:
            if n in candidates:
                return n
        return candidates[0]

    # 組合：每區至少1碼，熱號優先，補冷號1碼，尾數穿插
    picked = []
    for zone_nums in zones.values():
        n = best_in_zone(zone_nums, set(picked))
        if n:
            picked.append(n)

    # 補到5碼：先嘗試加入冷號，再從熱號補
    for n in cold_nums:
        if len(picked) >= 5:
            break
        if n not in picked:
            picked.append(n)
    for n in tail_candidates + hot_nums:
        if len(picked) >= 5:
            break
        if n not in picked:
            picked.append(n)
    # 最後從全體補齊
    for n in BALL_RANGE:
        if len(picked) >= 5:
            break
        if n not in picked:
            picked.append(n)

    # 嘗試找通過過濾的排列（最多嘗試100組）
    import itertools
    candidates_pool = list(set(
        hot_nums[:6] + cold_nums[:3] + tail_candidates[:6] + picked
    ))
    result = sorted(picked[:5])
    for combo in itertools.combinations(candidates_pool, 5):
        if _ok(list(combo)):
            result = sorted(combo)
            break

    # 分析說明
    used_hot  = [n for n in result if n in hot_nums[:5]]
    used_cold = [n for n in result if n in cold_nums]
    used_tail = [(n, n % 10) for n in result if n in tail_candidates]
    zone_dist = {
        "低(1-13)":   [n for n in result if 1  <= n <= 13],
        "中(14-26)":  [n for n in result if 14 <= n <= 26],
        "高(27-39)":  [n for n in result if 27 <= n <= 39],
    }

    return {
        "nums":      result,
        "hot_used":  used_hot,
        "cold_used": used_cold,
        "tail_used": used_tail,
        "zone_dist": zone_dist,
        "cold_top3": cold_nums[:3],
        "hot_tail":  [{"tail": t, "cnt": c} for t, c in hot_tail],
    }


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
