"""
Bingo Bingo 自主演算法學習模組（深度迭代版）
────────────────────────────────────────────────────────
Bingo 特性：
  - 每期從 1–80 開出 20 顆球（選中率天然較高）
  - 6星選 6 個：≥4 中才有獎（目標：平均命中 3 顆以上）
  - 9星選 9 個：≥6 中才有獎（目標：平均命中 4 顆以上）
  - 每 5 分鐘一期，每日 270+ 期，資料量大

學習架構（每日迭代 + 每週深度學習）：
  每日：
    1. 熱度更新：近50期 vs 近500期頻率計算基礎權重
    2. 冷號補正：遺漏期數越長，補正倍率越高
    3. 命中回饋：昨日快照實際命中號碼 → 強化這些號碼的下次推薦
    4. 時段偏好：各時段（12/17/20點）號碼出現率差異，分開學習
    5. 數對學習：高命中期的推薦數對 → 加強其關聯權重
  每週（週一深度學習）：
    6. 趨勢分析：本週 vs 上週命中率對比，識別改善或退步
    7. 冷熱週期更新：分析週期性規律（哪些號碼有固定週期出現）
    8. 低命中號碼懲罰：連續三週低於平均 → 降低其推薦優先度
    9. 保存週報快照到歷史檔（持久化備查）
"""

import json
import pytz
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

_TW = pytz.timezone("Asia/Taipei")
_DATA_DIR    = Path("/data") if Path("/data").exists() else Path(__file__).parent
LEARN_FILE   = _DATA_DIR / "bingo_learn.json"
WEEKLY_FILE  = _DATA_DIR / "bingo_learn_weekly.json"   # 每週深度學習歷史

NUM_RANGE    = list(range(1, 81))
SHORT_WIN    = 50
LONG_WIN     = 500
MISS_BOOST_MAX = 1.8
SLOT_LABELS  = ["12:00", "17:00", "20:00"]

# 目標命中數
TARGET_SIX  = 3.0
TARGET_NINE = 4.0


# ── 基礎 I/O ──────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "weights":      {str(n): 1.0 for n in NUM_RANGE},
        "slot_hits":    {slot: {str(n): 0 for n in NUM_RANGE} for slot in SLOT_LABELS},
        "pair_weights": {},
        # 號碼連續低命中懲罰計數 {str(n): int}（每週更新）
        "penalty_counts": {str(n): 0 for n in NUM_RANGE},
        "history":       [],   # 每日記錄
        "total_rounds":  0,
        "avg_six_hits":  0.0,
        "avg_nine_hits": 0.0,
        "last_updated":  None,
    }


def load_state() -> dict:
    if LEARN_FILE.exists():
        try:
            s = json.loads(LEARN_FILE.read_text())
            for k, v in _default_state().items():
                if k not in s:
                    s[k] = v
            return s
        except Exception:
            pass
    return _default_state()


def save_state(state: dict):
    LEARN_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _load_weekly_history() -> list:
    if WEEKLY_FILE.exists():
        try:
            return json.loads(WEEKLY_FILE.read_text())
        except Exception:
            pass
    return []


def _save_weekly_history(data: list):
    WEEKLY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def get_weights() -> dict[int, float]:
    state = load_state()
    return {int(k): v for k, v in state["weights"].items()}


def get_pair_weights() -> dict[tuple, float]:
    state = load_state()
    result = {}
    for key, w in state.get("pair_weights", {}).items():
        a, b = map(int, key.split(","))
        result[(a, b)] = w
    return result


# ── 基礎權重計算 ──────────────────────────────────────────────────────────────

def _calc_base_weights(df) -> dict[int, float]:
    """短期熱度 × 長期基線 × 冷號遺漏補正"""
    import bingo_core
    num_cols = bingo_core.NUM_COLS

    short_df = df.tail(SHORT_WIN)
    long_df  = df.tail(LONG_WIN)

    short_cnt = Counter(short_df[num_cols].values.flatten().tolist())
    long_cnt  = Counter(long_df[num_cols].values.flatten().tolist())

    short_avg = sum(short_cnt.values()) / len(NUM_RANGE)
    long_avg  = sum(long_cnt.values())  / len(NUM_RANGE)

    all_periods = df[num_cols].values.tolist()
    last_seen = {}
    for i, row in enumerate(all_periods):
        for n in row:
            last_seen[int(n)] = i
    total = len(all_periods)

    weights = {}
    for n in NUM_RANGE:
        short_ratio = short_cnt.get(n, 0) / short_avg if short_avg else 1.0
        long_ratio  = long_cnt.get(n, 0) / long_avg   if long_avg  else 1.0
        miss = total - 1 - last_seen.get(n, 0)
        expected_gap = len(NUM_RANGE) / 20
        miss_ratio = min(MISS_BOOST_MAX, 1.0 + 0.7 * (miss - expected_gap) / max(expected_gap, 1))
        miss_ratio = max(0.7, miss_ratio)

        weights[n] = (short_ratio * 0.65 + long_ratio * 0.35) * miss_ratio

    avg = sum(weights.values()) / len(weights)
    if avg > 0:
        weights = {n: w / avg for n, w in weights.items()}
    return weights


def _apply_slot_bias(weights: dict[int, float], slot: str, slot_hits: dict) -> dict[int, float]:
    if slot not in slot_hits:
        return weights
    sh = slot_hits[slot]
    max_hit = max(sh.values()) or 1
    result = {}
    for n in NUM_RANGE:
        bias = 1.0 + 0.35 * (sh.get(str(n), 0) / max_hit)
        result[n] = weights[n] * bias
    avg = sum(result.values()) / len(result)
    return {n: w / avg for n, w in result.items()} if avg else result


# ── 每日迭代 ──────────────────────────────────────────────────────────────────

def daily_update(tracker_data: dict, df=None) -> dict:
    """
    每日 00:05 呼叫：
    1. 重算基礎權重（熱度 + 遺漏補正）
    2. 昨日快照命中回饋 → 強化命中號碼
    3. 更新時段偏好 + 數對學習
    4. 合併全局推薦權重
    """
    yesterday  = (datetime.now(_TW) - timedelta(days=1)).strftime("%Y-%m-%d")
    yest_snaps = [
        s for s in tracker_data.get("snapshots", [])
        if s.get("date") == yesterday and s.get("settled")
    ]

    state = load_state()

    # Step 1：重算基礎權重
    if df is not None and not df.empty:
        base_w = _calc_base_weights(df)
        state["weights"] = {str(n): base_w[n] for n in NUM_RANGE}

    total_six_hits  = 0
    total_nine_hits = 0
    round_count     = 0

    pair_weights = state.get("pair_weights", {})
    penalty      = state.get("penalty_counts", {str(n): 0 for n in NUM_RANGE})

    # 每日命中號碼累計（用於強化）
    hit_six_counter  = Counter()
    hit_nine_counter = Counter()

    # ── 從 df 取昨日實際開獎號碼（精準學習）──────────────────────
    import bingo_core as _bc
    yest_actual_counter = Counter()   # 號碼在昨日各期出現次數
    if df is not None and not df.empty:
        try:
            yest_rows = df[df["date"] == yesterday]
            for _, row in yest_rows.iterrows():
                for col in _bc.NUM_COLS:
                    yest_actual_counter[int(row[col])] += 1
        except Exception:
            pass

    # 昨日實際開獎加成：出現次數越多 → 越應該在推薦中
    if yest_actual_counter:
        max_cnt = max(yest_actual_counter.values())
        for n, cnt in yest_actual_counter.items():
            boost = 1.0 + 0.12 * (cnt / max_cnt)
            state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * boost

    # Step 2：昨日快照命中回饋
    for snap in yest_snaps:
        slot = snap.get("slot", "")
        if slot not in SLOT_LABELS:
            continue
        six  = snap.get("six",  [])
        nine = snap.get("nine", [])
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

            # 命中回饋：根據命中率強化/懲罰推薦號碼
            six_rate  = six_hits  / 6
            nine_rate = nine_hits / 9

            # 六星命中回饋
            if six_hits >= 3:
                for n in six:
                    factor = 1.0 + 0.10 * six_hits
                    state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * factor
            elif six_hits <= 1:
                # 低命中 → 輕微懲罰推薦號碼（但不過度）
                for n in six:
                    state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * 0.96

            # 九星命中回饋
            if nine_hits >= 4:
                for n in nine:
                    factor = 1.0 + 0.07 * nine_hits
                    state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * factor
            elif nine_hits <= 2:
                for n in nine:
                    state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * 0.97

            # 時段偏好更新（降低門檻：六星≥3 / 九星≥5）
            if six_hits >= 3 and slot in state["slot_hits"]:
                for n in six:
                    state["slot_hits"][slot][str(n)] = \
                        state["slot_hits"][slot].get(str(n), 0) + six_hits

            if nine_hits >= 5 and slot in state["slot_hits"]:
                for n in nine:
                    state["slot_hits"][slot][str(n)] = \
                        state["slot_hits"][slot].get(str(n), 0) + nine_hits

            # 數對學習（整體命中率）
            hit_rate = (six_rate + nine_rate) / 2
            PAIR_BONUS = 1.12
            PAIR_DECAY = 0.97
            for (a, b) in rec_pairs:
                key = f"{a},{b}"
                w = pair_weights.get(key, 1.0)
                if hit_rate >= 0.35:
                    w = min(3.0, w * PAIR_BONUS)
                else:
                    w = max(0.3, w * PAIR_DECAY)
                pair_weights[key] = round(w, 4)

    state["pair_weights"] = pair_weights

    # Step 3：正規化權重
    vals = [float(v) for v in state["weights"].values()]
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: float(v) / avg for k, v in state["weights"].items()}

    # Step 4：融合時段偏好
    base_weights = {int(k): v for k, v in state["weights"].items()}
    blended = {}
    for slot in SLOT_LABELS:
        w = _apply_slot_bias(base_weights, slot, state["slot_hits"])
        for n, v in w.items():
            blended[n] = blended.get(n, 0) + v / len(SLOT_LABELS)
    avg2 = sum(blended.values()) / len(blended)
    if avg2 > 0:
        state["weights"] = {str(n): blended[n] / avg2 for n in NUM_RANGE}

    # 記錄
    day_avg6  = round(total_six_hits  / round_count, 2) if round_count else 0.0
    day_avg9  = round(total_nine_hits / round_count, 2) if round_count else 0.0
    record = {
        "date":          yesterday,
        "snaps":         len(yest_snaps),
        "rounds":        round_count,
        "avg_six_hits":  day_avg6,
        "avg_nine_hits": day_avg9,
    }
    state["history"].append(record)
    state["history"]     = state["history"][-90:]
    state["total_rounds"] += round_count
    state["last_updated"]  = datetime.now(_TW).strftime("%Y-%m-%d %H:%M")

    recent14 = state["history"][-14:]
    state["avg_six_hits"]  = round(sum(r["avg_six_hits"]  for r in recent14) / len(recent14), 2) if recent14 else 0.0
    state["avg_nine_hits"] = round(sum(r["avg_nine_hits"] for r in recent14) / len(recent14), 2) if recent14 else 0.0

    state["penalty_counts"] = penalty
    save_state(state)

    return {
        "updated":       True,
        "date":          yesterday,
        "snaps":         len(yest_snaps),
        "rounds":        round_count,
        "avg_six_hits":  day_avg6,
        "avg_nine_hits": day_avg9,
    }


# ── 每週深度學習 ──────────────────────────────────────────────────────────────

def weekly_deep_learn(df=None) -> dict:
    """
    每週一執行深度學習：
    1. 計算本週 vs 上週命中率變化
    2. 週期性分析：哪些號碼有固定週期出現
    3. 連續低命中號碼懲罰（連3週低於均值 → 降權重 10%）
    4. 儲存週報快照（永久保存）
    5. 回傳報告文字供 TG 推播
    """
    state   = load_state()
    history = state.get("history", [])

    # 本週 / 上週記錄
    this_week = history[-7:] if len(history) >= 7  else history
    last_week = history[-14:-7] if len(history) >= 14 else []

    def avg_hits(lst, key):
        return round(sum(r.get(key, 0) for r in lst) / len(lst), 2) if lst else 0.0

    tw_six  = avg_hits(this_week, "avg_six_hits")
    tw_nine = avg_hits(this_week, "avg_nine_hits")
    lw_six  = avg_hits(last_week, "avg_six_hits")
    lw_nine = avg_hits(last_week, "avg_nine_hits")

    delta_six  = round(tw_six  - lw_six,  2)
    delta_nine = round(tw_nine - lw_nine, 2)

    # 週期性分析（近 200 期開獎，找每個號碼的平均間隔）
    period_analysis = {}
    if df is not None and not df.empty:
        import bingo_core
        num_cols = bingo_core.NUM_COLS
        recent200 = df.tail(200)
        all_rows  = recent200[num_cols].values.tolist()
        for n in NUM_RANGE:
            positions = [i for i, row in enumerate(all_rows) if n in [int(x) for x in row]]
            if len(positions) >= 3:
                gaps = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
                avg_gap = sum(gaps) / len(gaps)
                # 距上次出現
                last_pos = positions[-1] if positions else 0
                since    = len(all_rows) - 1 - last_pos
                # 若 since ≥ avg_gap → 即將出現
                period_analysis[n] = {
                    "avg_gap": round(avg_gap, 1),
                    "since":   since,
                    "due":     since >= avg_gap * 0.9,
                }

    # 即將出現的號碼（前15名）
    due_nums = sorted(
        [n for n, v in period_analysis.items() if v["due"]],
        key=lambda n: period_analysis[n]["since"] / max(period_analysis[n]["avg_gap"], 1),
        reverse=True,
    )[:15]

    # 連續低命中懲罰
    penalty = state.get("penalty_counts", {str(n): 0 for n in NUM_RANGE})
    overall_avg6 = state.get("avg_six_hits", 0)
    if tw_six < max(overall_avg6 * 0.8, 0.1):
        # 本週六星命中率低 → 增加懲罰計數
        # 找近7天中命中次數少的推薦號碼（用 slot_hits 代理）
        slot_total = {str(n): 0 for n in NUM_RANGE}
        for slot, sh in state.get("slot_hits", {}).items():
            for k, v in sh.items():
                slot_total[k] = slot_total.get(k, 0) + v
        median_hits = sorted(slot_total.values())[len(NUM_RANGE)//2]
        for n in NUM_RANGE:
            if slot_total.get(str(n), 0) < median_hits * 0.5:
                penalty[str(n)] = penalty.get(str(n), 0) + 1
            else:
                penalty[str(n)] = max(0, penalty.get(str(n), 0) - 1)

    # 懲罰超過3次的號碼降低權重
    weights = state["weights"]
    for n in NUM_RANGE:
        cnt = penalty.get(str(n), 0)
        if cnt >= 3:
            weights[str(n)] = float(weights.get(str(n), 1.0)) * 0.88
    # 同時提升即將到來的號碼
    for n in due_nums:
        weights[str(n)] = float(weights.get(str(n), 1.0)) * 1.12

    # 重新正規化
    vals = [float(v) for v in weights.values()]
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: float(v) / avg for k, v in weights.items()}

    state["penalty_counts"] = penalty
    save_state(state)

    # 組週報文字
    gap6  = round(TARGET_SIX  - tw_six,  2)
    gap9  = round(TARGET_NINE - tw_nine, 2)
    lines = [
        "📊 Bingo 週度深度學習報告",
        "═" * 36,
        f"🗓️  本週平均命中（最近{len(this_week)}日）",
        f"   6星：{tw_six:.2f} 顆  ({'+' if delta_six>=0 else ''}{delta_six:.2f} vs 上週)",
        f"   9星：{tw_nine:.2f} 顆  ({'+' if delta_nine>=0 else ''}{delta_nine:.2f} vs 上週)",
        f"",
        f"🎯 距目標差距",
        f"   6星目標 {TARGET_SIX} 顆 → 差 {gap6:.2f} 顆",
        f"   9星目標 {TARGET_NINE} 顆 → 差 {gap9:.2f} 顆",
        f"",
        f"🔄 本週深度學習調整",
        f"   週期分析即將出現號碼（前10）：{due_nums[:10]}",
        f"   連續低命中懲罰號碼數：{sum(1 for v in penalty.values() if v >= 3)} 個",
        f"",
        f"📊 14日累計平均命中",
        f"   6星：{state['avg_six_hits']:.2f} / 9星：{state['avg_nine_hits']:.2f}",
        f"   累計學習期數：{state['total_rounds']}",
        f"═" * 36,
    ]
    report_text = "\n".join(lines)

    # 保存週報快照（永久歷史）
    weekly_hist = _load_weekly_history()
    snap = {
        "week_end":   datetime.now(_TW).strftime("%Y-%m-%d"),
        "this_week":  {"six": tw_six, "nine": tw_nine},
        "last_week":  {"six": lw_six, "nine": lw_nine},
        "delta":      {"six": delta_six, "nine": delta_nine},
        "due_nums":   due_nums[:10],
        "total_rounds": state["total_rounds"],
        "report_text": report_text,
    }
    weekly_hist.append(snap)
    _save_weekly_history(weekly_hist)

    return {
        "ok":          True,
        "this_week":   {"six": tw_six, "nine": tw_nine},
        "last_week":   {"six": lw_six, "nine": lw_nine},
        "delta":       {"six": delta_six, "nine": delta_nine},
        "due_nums":    due_nums[:10],
        "total_rounds": state["total_rounds"],
        "report_text": report_text,
    }


def get_weights_for_slot(slot: str) -> dict[int, float]:
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


def get_weekly_history() -> list:
    """取得所有週報歷史（永久保存）"""
    return _load_weekly_history()
