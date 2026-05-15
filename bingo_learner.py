"""
Bingo Bingo 自主演算法學習模組
────────────────────────────────────────────────────────
Bingo 特性：
  - 每期從 1–80 開出 20 顆球（命中率天然較高）
  - 6星選 6 個：≥4 中才有獎
  - 9星選 9 個：≥6 中才有獎
  - 每 5 分鐘一期，每日 270+ 期，資料量大

學習策略：
  1. 號碼熱度追蹤：近期開出頻率（短窗口 50 期 vs 長窗口 500 期）
  2. 冷號補正：久未出現的號碼依遺漏期數給予反彈加成
  3. 時段傾向：不同時段（12/17/20點）各號碼出現率有差異，分開學習
  4. 命中回饋：根據昨日快照的實際對獎，正向強化命中號碼的時段偏好

每日 09:00 自動更新，smart_pick 下次推薦自動套用。
"""

import json
import pytz
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

_TW = pytz.timezone("Asia/Taipei")
_DATA_DIR  = Path("/data") if Path("/data").exists() else Path(__file__).parent
LEARN_FILE = _DATA_DIR / "bingo_learn.json"

NUM_RANGE = list(range(1, 81))   # 1–80
SHORT_WIN = 50                    # 短期熱度窗口（期）
LONG_WIN  = 500                   # 長期基線窗口（期）
MISS_BOOST_MAX = 1.6              # 冷號最大補正倍率
SLOT_LABELS = ["12:00", "17:00", "20:00"]


# ── 基礎 I/O ──────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        # 全局號碼權重（1–80）
        "weights": {str(n): 1.0 for n in NUM_RANGE},
        # 各時段號碼偏好（記錄各時段命中次數）
        "slot_hits": {
            slot: {str(n): 0 for n in NUM_RANGE}
            for slot in SLOT_LABELS
        },
        # 強力關聯數對的迭代學習權重
        # key = "a,b"（a < b），value = float（>1 代表此對歷史命中率佳）
        "pair_weights": {},
        # 每日學習記錄
        "history": [],
        "total_rounds": 0,
        "avg_six_hits": 0.0,
        "avg_nine_hits": 0.0,
        "last_updated": None,
    }


def load_state() -> dict:
    if LEARN_FILE.exists():
        try:
            state = json.loads(LEARN_FILE.read_text())
            # 補齊缺少的欄位（版本升級相容）
            default = _default_state()
            for k, v in default.items():
                if k not in state:
                    state[k] = v
            return state
        except Exception:
            pass
    return _default_state()


def save_state(state: dict):
    LEARN_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_weights() -> dict[int, float]:
    """回傳 {號碼: 學習權重}，供 smart_pick 使用"""
    state = load_state()
    return {int(k): v for k, v in state["weights"].items()}


def get_pair_weights() -> dict[tuple, float]:
    """回傳 {(a,b): 學習權重}，供 smart_pick 納入數對迭代分數"""
    state = load_state()
    result = {}
    for key, w in state.get("pair_weights", {}).items():
        a, b = map(int, key.split(","))
        result[(a, b)] = w
    return result


# ── 核心學習邏輯 ──────────────────────────────────────────────────────────────

def _calc_base_weights(df) -> dict[int, float]:
    """
    從開獎資料計算基礎號碼權重：
    短期熱度（近50期） × 長期基線（近500期）× 冷號遺漏補正
    """
    import bingo_core
    num_cols = bingo_core.NUM_COLS

    short_df = df.tail(SHORT_WIN)
    long_df  = df.tail(LONG_WIN)

    short_cnt = Counter(short_df[num_cols].values.flatten().tolist())
    long_cnt  = Counter(long_df[num_cols].values.flatten().tolist())

    short_avg = sum(short_cnt.values()) / len(NUM_RANGE)
    long_avg  = sum(long_cnt.values())  / len(NUM_RANGE)

    # 遺漏期數：找每個號碼最近一次出現
    all_periods = df[num_cols].values.tolist()
    last_seen = {}
    for i, row in enumerate(all_periods):
        for n in row:
            last_seen[int(n)] = i
    total = len(all_periods)

    weights = {}
    for n in NUM_RANGE:
        # 短期熱度比例（>1 代表比平均熱）
        short_ratio = short_cnt.get(n, 0) / short_avg if short_avg else 1.0
        # 長期基線（穩定性）
        long_ratio  = long_cnt.get(n, 0) / long_avg if long_avg else 1.0
        # 冷號遺漏補正（最近未出現的期數越多，補正越大）
        miss = total - 1 - last_seen.get(n, 0)
        expected_gap = len(NUM_RANGE) / 20   # 平均每 4 期出現一次
        miss_ratio = min(MISS_BOOST_MAX, 1.0 + 0.6 * (miss - expected_gap) / max(expected_gap, 1))
        miss_ratio = max(0.8, miss_ratio)

        weights[n] = short_ratio * 0.6 + long_ratio * 0.4
        weights[n] *= miss_ratio

    # 正規化
    avg = sum(weights.values()) / len(weights)
    if avg > 0:
        weights = {n: w / avg for n, w in weights.items()}

    return weights


def _apply_slot_bias(weights: dict[int, float], slot: str, slot_hits: dict) -> dict[int, float]:
    """將時段命中偏好疊加到基礎權重上"""
    if slot not in slot_hits:
        return weights
    sh = slot_hits[slot]
    max_hit = max(sh.values()) or 1
    result = {}
    for n in NUM_RANGE:
        bias = 1.0 + 0.3 * (sh.get(str(n), 0) / max_hit)
        result[n] = weights[n] * bias
    # 重新正規化
    avg = sum(result.values()) / len(result)
    return {n: w / avg for n, w in result.items()} if avg else result


def daily_update(tracker_data: dict, df=None):
    """
    每日 09:00 呼叫：
    1. 從開獎資料重算基礎權重（熱度 + 冷號補正）
    2. 根據昨日快照命中情況更新時段偏好
    3. 合併產生新的全局推薦權重
    """
    yesterday = (datetime.now(_TW) - timedelta(days=1)).strftime("%Y-%m-%d")
    yest_snaps = [
        s for s in tracker_data.get("snapshots", [])
        if s.get("date") == yesterday and s.get("settled")
    ]

    state = load_state()

    # Step 1：從最新開獎資料重算基礎權重
    if df is not None and not df.empty:
        base_w = _calc_base_weights(df)
        state["weights"] = {str(n): base_w[n] for n in NUM_RANGE}

    total_six_hits  = 0
    total_nine_hits = 0
    round_count     = 0

    # Step 2：昨日各快照的命中回饋，更新時段偏好 + 數對學習
    pair_weights = state.get("pair_weights", {})
    PAIR_HIT_BONUS  = 1.15   # 數對同期命中 → 權重 ×1.15
    PAIR_MISS_DECAY = 0.97   # 數對未同期命中 → 衰減
    PAIR_MAX        = 3.0    # 數對權重上限
    PAIR_MIN        = 0.3    # 數對權重下限

    for snap in yest_snaps:
        slot = snap.get("slot", "")
        if slot not in SLOT_LABELS:
            continue
        six  = snap.get("six",  [])
        nine = snap.get("nine", [])

        # 建立本快照推薦的所有數對（6星+9星聯集）
        all_rec = list(set(six + nine))
        rec_pairs = []
        for i in range(len(all_rec)):
            for j in range(i + 1, len(all_rec)):
                a, b = sorted([all_rec[i], all_rec[j]])
                rec_pairs.append((a, b))

        for result in snap.get("results", []):
            six_hits  = result.get("six_hits",  0)
            nine_hits = result.get("nine_hits", 0)
            total_six_hits  += six_hits
            total_nine_hits += nine_hits
            round_count += 1

            # 命中越多 → 強化這些號碼在本時段的偏好
            if six_hits >= 4:
                for n in six:
                    state["slot_hits"][slot][str(n)] = \
                        state["slot_hits"][slot].get(str(n), 0) + six_hits
            if nine_hits >= 6:
                for n in nine:
                    state["slot_hits"][slot][str(n)] = \
                        state["slot_hits"][slot].get(str(n), 0) + nine_hits

            # 數對迭代學習：比對推薦數對是否在本期實際開獎中同時出現
            # period 實際開獎號碼需從 df 取得（用 result 的 period 對照）
            # 這裡用 six_hits + nine_hits 的高低作為代理信號：
            # 命中數越高 → 推薦號碼整體表現好 → 強化所有推薦數對
            hit_rate = (six_hits / 6 + nine_hits / 9) / 2  # 0.0~1.0
            for (a, b) in rec_pairs:
                key = f"{a},{b}"
                w = pair_weights.get(key, 1.0)
                if hit_rate >= 0.4:   # 整體命中率 ≥ 40%，視為數對表現佳
                    w = min(PAIR_MAX, w * PAIR_HIT_BONUS)
                else:
                    w = max(PAIR_MIN, w * PAIR_MISS_DECAY)
                pair_weights[key] = round(w, 4)

    state["pair_weights"] = pair_weights

    # Step 3：將時段偏好平均融入全局權重（供預設推薦使用）
    base_weights = {int(k): v for k, v in state["weights"].items()}
    blended = {}
    for slot in SLOT_LABELS:
        w = _apply_slot_bias(base_weights, slot, state["slot_hits"])
        for n, v in w.items():
            blended[n] = blended.get(n, 0) + v / len(SLOT_LABELS)

    avg = sum(blended.values()) / len(blended)
    if avg > 0:
        state["weights"] = {str(n): blended[n] / avg for n in NUM_RANGE}

    # 記錄
    record = {
        "date":           yesterday,
        "snaps":          len(yest_snaps),
        "rounds":         round_count,
        "avg_six_hits":   round(total_six_hits  / round_count, 2) if round_count else 0,
        "avg_nine_hits":  round(total_nine_hits / round_count, 2) if round_count else 0,
    }
    state["history"].append(record)
    state["history"] = state["history"][-60:]
    state["total_rounds"] += round_count
    state["last_updated"]  = datetime.now(_TW).strftime("%Y-%m-%d %H:%M")

    recent = state["history"][-14:]
    state["avg_six_hits"]  = round(sum(r["avg_six_hits"]  for r in recent) / len(recent), 2)
    state["avg_nine_hits"] = round(sum(r["avg_nine_hits"] for r in recent) / len(recent), 2)

    save_state(state)
    return {
        "updated":       True,
        "date":          yesterday,
        "snaps":         len(yest_snaps),
        "rounds":        round_count,
        "avg_six_hits":  record["avg_six_hits"],
        "avg_nine_hits": record["avg_nine_hits"],
    }


def get_weights_for_slot(slot: str) -> dict[int, float]:
    """取特定時段的號碼權重（比全局更精準）"""
    state = load_state()
    base  = {int(k): v for k, v in state["weights"].items()}
    return _apply_slot_bias(base, slot, state["slot_hits"])


def get_summary() -> dict:
    state = load_state()
    return {
        "total_rounds":  state["total_rounds"],
        "avg_six_hits":  state["avg_six_hits"],
        "avg_nine_hits": state["avg_nine_hits"],
        "last_updated":  state["last_updated"],
        "history":       state["history"][-7:],
    }
