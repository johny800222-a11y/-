"""
今彩539 核心邏輯（資料抓取 + 推薦演算法）
策略A (prob)   : 全期頻率 × 學習權重 × 遺漏修正（長期穩定）
策略B (value)  : 近20期短期頻率 + 號碼對相關 + 連號偵測（短期追熱）
策略C (strategy): 冷熱號 + 尾數群聚 + 區間分散（結構化平衡）
"""

import re
import time
import random
import itertools
from pathlib import Path
from datetime import datetime
from collections import Counter

import requests
from bs4 import BeautifulSoup
import pandas as pd

_DATA_DIR   = Path("/data") if Path("/data").exists() else Path(__file__).parent
DATA_FILE   = _DATA_DIR / "539_history.csv"
BASE_URL    = "https://www.pilio.idv.tw/lto539/list539BIG.asp"
TOTAL_PAGES = 256
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


def get_total_pages() -> int:
    try:
        resp = requests.get(f"{BASE_URL}?indexpage=1&orderby=old", headers=HEADERS, timeout=15)
        resp.encoding = "big5"
        pages = re.findall(r'indexpage=(\d+)', resp.text)
        if pages:
            return max(int(p) for p in pages)
    except Exception:
        pass
    return TOTAL_PAGES


def fetch_all(progress_cb=None) -> pd.DataFrame:
    total = get_total_pages()
    all_records = []
    for page in range(1, total + 1):
        try:
            all_records.extend(_fetch_page(page))
            if progress_cb:
                progress_cb(page, total)
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
    last_page = get_total_pages()
    new_records = _fetch_page(last_page - 1) + _fetch_page(last_page)
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


# ── 和值統計（從歷史資料動態計算）────────────────────────────────────────────

_SUM_RANGE_CACHE: tuple | None = None   # (p15, p85) 懶加載快取

def _get_sum_range(df: pd.DataFrame) -> tuple[int, int]:
    """
    從歷史資料計算 5 顆號碼和值的 15th~85th percentile（動態更新）。
    539 歷史和值通常集中在 65~125 之間，使用百分位讓它跟著資料走。
    """
    global _SUM_RANGE_CACHE
    if _SUM_RANGE_CACHE is not None:
        return _SUM_RANGE_CACHE
    try:
        sums = sorted(
            int(row["n1"]) + int(row["n2"]) + int(row["n3"]) + int(row["n4"]) + int(row["n5"])
            for _, row in df.tail(500).iterrows()
        )
        n = len(sums)
        p15 = sums[max(0, int(n * 0.15))]
        p85 = sums[min(n - 1, int(n * 0.85))]
        _SUM_RANGE_CACHE = (p15, p85)
        return _SUM_RANGE_CACHE
    except Exception:
        return (65, 125)   # 保守預設


def _apply_sum_constraint(nums: list[int], df: pd.DataFrame,
                          pool_weights: dict[int, float]) -> list[int]:
    """
    和值約束後處理：若5顆號碼的和不在歷史典型範圍，
    替換一顆讓和值回到合理區間，用 pool_weights 選最佳替換球。
    最多嘗試 3 輪替換，避免破壞其他特性。
    """
    lo, hi = _get_sum_range(df)
    result = list(nums)

    for _ in range(3):
        s = sum(result)
        if lo <= s <= hi:
            break

        if s < lo:
            # 和太小 → 換掉最小的球，換成更大的
            target_n = min(result)
            result.remove(target_n)
            need_add = lo - sum(result)   # 需要補多少
            candidates = sorted(
                [n for n in BALL_RANGE if n not in result and n > target_n],
                key=lambda n: (abs(n - need_add - 0) , -pool_weights.get(n, 0))
            )
        else:
            # 和太大 → 換掉最大的球，換成更小的
            target_n = max(result)
            result.remove(target_n)
            need_sub = sum(result) + target_n - hi   # 需要減多少
            candidates = sorted(
                [n for n in BALL_RANGE if n not in result and n < target_n],
                key=lambda n: (abs(target_n - n - need_sub), -pool_weights.get(n, 0))
            )

        if candidates:
            result.append(candidates[0])

    return sorted(result)


# ── 共用過濾 ──────────────────────────────────────────────────────────────────

def _ok(nums: list[int], df: pd.DataFrame = None) -> bool:
    if len(set(nums)) != 5:
        return False
    odds = sum(1 for n in nums if n % 2 != 0)
    s    = sum(nums)
    if odds not in (2, 3):
        return False
    # 動態和值範圍（有 df 時用歷史計算，沒有用保守預設）
    lo, hi = _get_sum_range(df) if df is not None else (60, 140)
    return lo <= s <= hi


# ── 策略A：機率推薦（長期頻率 × 學習權重 × 遺漏修正）───────────────────────

def recommend_best(df: pd.DataFrame, learn_weights: dict[int, float] | None = None) -> list[int]:
    """
    策略A：大數據綜合推薦
    歷史頻率（長期）× 學習權重（近期命中修正）× 遺漏值修正（近50期）× 連號傾向
    """
    freq     = Counter(df[BALL_COLS].values.flatten().tolist())
    total    = sum(freq.values())
    avg_freq = total / len(BALL_RANGE)

    recent50 = Counter(df.tail(50)[BALL_COLS].values.flatten().tolist())
    avg_r50  = sum(recent50.values()) / len(BALL_RANGE)

    pool    = sorted(BALL_RANGE)
    weights = []
    for n in pool:
        w = freq.get(n, 0) / avg_freq
        w *= (learn_weights.get(n, 1.0) if learn_weights else 1.0)
        miss_bonus = 1.0 + 0.5 * max(0, avg_r50 - recent50.get(n, 0)) / max(avg_r50, 1)
        w *= miss_bonus
        adj = recent50.get(n - 1, 0) + recent50.get(n + 1, 0)
        w *= min(1.4, 1.0 + 0.2 * adj / max(avg_r50, 1))
        weights.append(max(w, 0.01))

    pw = {n: weights[i] for i, n in enumerate(pool)}
    for _ in range(50000):
        chosen = sorted(set(random.choices(pool, weights=weights, k=10)))[:5]
        if len(chosen) == 5 and _ok(chosen, df):
            return _apply_sum_constraint(chosen, df, pw)
    return sorted(random.sample(BALL_RANGE, 5))


# ── 策略B：價值推薦（近期短波 + 號碼對相關 + 連號特徵）─────────────────────

def value_recommend(df: pd.DataFrame, learn_weights: dict[int, float] | None = None) -> list[int]:
    """
    策略B：短期熱區追蹤
    - 近20期高頻號碼為主力（短期波動追蹤）
    - 號碼對共現分析：選與最近一期共現率最高的號碼
    - 連號偵測：如近5期出現連號對，優先納入
    - 學習權重輔助排序
    """
    recent20 = df.tail(20)
    recent5  = df.tail(5)

    # 近20期頻率
    freq20 = Counter(recent20[BALL_COLS].values.flatten().tolist())

    # 近60期號碼對共現（找關聯度高的對子）
    recent60 = df.tail(60)
    pair_count: dict[tuple, int] = {}
    for _, row in recent60.iterrows():
        nums = [int(row[c]) for c in BALL_COLS]
        for a, b in itertools.combinations(sorted(nums), 2):
            pair_count[(a, b)] = pair_count.get((a, b), 0) + 1

    # 最近一期的號碼 → 找和它共現次數最高的夥伴
    last_nums = [int(recent5.iloc[-1][c]) for c in BALL_COLS]
    partner_score: dict[int, int] = {}
    for n in last_nums:
        for (a, b), cnt in pair_count.items():
            if a == n:
                partner_score[b] = partner_score.get(b, 0) + cnt
            elif b == n:
                partner_score[a] = partner_score.get(a, 0) + cnt

    # 連號偵測：近5期出現過的連號對，給加分
    consec_bonus: dict[int, float] = {}
    for _, row in recent5.iterrows():
        nums = sorted([int(row[c]) for c in BALL_COLS])
        for i in range(len(nums) - 1):
            if nums[i + 1] - nums[i] == 1:
                consec_bonus[nums[i]]     = consec_bonus.get(nums[i], 0) + 1.5
                consec_bonus[nums[i + 1]] = consec_bonus.get(nums[i + 1], 0) + 1.5

    # 合成權重
    pool    = sorted(BALL_RANGE)
    weights = []
    max_freq20  = max(freq20.values()) if freq20 else 1
    max_partner = max(partner_score.values()) if partner_score else 1

    for n in pool:
        w  = (freq20.get(n, 0) / max_freq20) * 3.0           # 近20期頻率（主信號）
        w += (partner_score.get(n, 0) / max(max_partner, 1)) * 2.0  # 號碼對關聯
        w += consec_bonus.get(n, 0)                           # 連號加分
        w *= (learn_weights.get(n, 1.0) if learn_weights else 1.0)
        weights.append(max(w, 0.05))

    pw = {n: weights[i] for i, n in enumerate(pool)}
    for _ in range(50000):
        chosen = sorted(set(random.choices(pool, weights=weights, k=10)))[:5]
        if len(chosen) == 5 and _ok(chosen, df):
            return _apply_sum_constraint(chosen, df, pw)
    return sorted(random.sample(BALL_RANGE, 5))


# ── 策略C：結構策略推薦（冷熱 + 尾數 + 區間）────────────────────────────────

def strategy_recommend(df: pd.DataFrame) -> dict:
    """
    策略C：結構化選號
    策略1 冷熱號碼：近10期熱號2碼 + 超過25期未出現冷號1碼
    策略2 尾數群聚：取近30期最熱門尾數，補入1碼同尾號
    策略3 分散區間：低(1-13)/中(14-26)/高(27-39) 各區保證至少1碼
    """
    recent10  = Counter(df.tail(10)[BALL_COLS].values.flatten().tolist())
    recent30  = Counter(df.tail(30)[BALL_COLS].values.flatten().tolist())

    hot_nums  = [n for n, _ in sorted(recent10.items(), key=lambda x: x[1], reverse=True)]

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

    tail_freq = Counter(n % 10 for n in recent30.elements())
    hot_tail  = tail_freq.most_common(2)
    tail_candidates = []
    for tail, _ in hot_tail:
        tail_candidates += [n for n in BALL_RANGE if n % 10 == tail]

    zones = {"低": list(range(1, 14)), "中": list(range(14, 27)), "高": list(range(27, 40))}

    def best_in_zone(zone_nums, exclude):
        candidates = [n for n in zone_nums if n not in exclude]
        if not candidates:
            return None
        for n in hot_nums:
            if n in candidates:
                return n
        for n in tail_candidates:
            if n in candidates:
                return n
        return candidates[0]

    picked = []
    for zone_nums in zones.values():
        n = best_in_zone(zone_nums, set(picked))
        if n:
            picked.append(n)

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
    for n in BALL_RANGE:
        if len(picked) >= 5:
            break
        if n not in picked:
            picked.append(n)

    candidates_pool = list(set(hot_nums[:6] + cold_nums[:3] + tail_candidates[:6] + picked))
    result = sorted(picked[:5])
    for combo in itertools.combinations(candidates_pool, 5):
        if _ok(list(combo), df):
            result = sorted(combo)
            break

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


# ── 整合推薦（根據近期各策略勝率，動態加權）────────────────────────────────

def ensemble_recommend(
    df: pd.DataFrame,
    learn_weights: dict[int, float] | None = None,
    strategy_scores: dict | None = None,
) -> list[int]:
    """
    整合三策略，依近30期各策略平均命中率做加權投票，
    選出得票最高的5碼作為最終推薦。
    strategy_scores = {"prob": float, "value": float, "strategy": float}
    """
    scores = strategy_scores or {}
    w_prob  = max(scores.get("prob", 1.0),     0.1)
    w_value = max(scores.get("value", 1.0),    0.1)
    w_strat = max(scores.get("strategy", 1.0), 0.1)

    rec_a = recommend_best(df, learn_weights)
    rec_b = value_recommend(df, learn_weights)
    rec_c = strategy_recommend(df)["nums"]

    # 計票
    vote: dict[int, float] = {}
    for n in rec_a:
        vote[n] = vote.get(n, 0) + w_prob
    for n in rec_b:
        vote[n] = vote.get(n, 0) + w_value
    for n in rec_c:
        vote[n] = vote.get(n, 0) + w_strat

    # 取票數前10，再從中過濾出5碼
    top10 = sorted(vote, key=vote.get, reverse=True)[:10]
    for combo in itertools.combinations(top10, 5):
        if _ok(list(combo), df):
            result = sorted(combo)
            return _apply_sum_constraint(result, df, vote)

    # fallback：直接取最高票5碼，再套和值約束
    base = sorted(top10[:5])
    return _apply_sum_constraint(base, df, vote)


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
