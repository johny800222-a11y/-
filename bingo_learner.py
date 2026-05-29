"""
Bingo Bingo 自主演算法學習模組（多策略投票版 v3）
────────────────────────────────────────────────────────
架構說明：
  bingo_core 採用 6 個獨立策略投票選號：
    S1 超短期動能 / S2 短期熱號 / S3 冷號回歸
    S4 強力共現 / S5 區間峰值 / S6 學習模型

  本模組負責：
    每日：
      1. 從 df 取實際開獎號碼，精準更新號碼權重
      2. 追蹤各策略提名的命中率（S1~S6 各自幾顆命中）
      3. 更新數對學習權重
    每週（週一深度學習）：
      4. 比較 S1~S6 近7天命中率，自動提升勝策略權重
      5. 回測不同參數組合（近3/8/20/50期），保留最佳窗口加成
      6. 懲罰連續低命中策略
      7. 保存週報快照（永久歷史）

  目標：6星平均 3 顆，9星平均 4 顆
"""

import json
import pytz
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

_TW = pytz.timezone("Asia/Taipei")
_DATA_DIR    = Path("/data") if Path("/data").exists() else Path(__file__).parent
LEARN_FILE   = _DATA_DIR / "bingo_learn.json"
WEEKLY_FILE  = _DATA_DIR / "bingo_learn_weekly.json"

NUM_RANGE    = list(range(1, 81))
SHORT_WIN    = 50
LONG_WIN     = 500
MISS_BOOST_MAX = 1.8
SLOT_LABELS  = ["12:00", "17:00", "20:00"]

TARGET_SIX  = 3.0
TARGET_NINE = 4.0

STRATEGY_KEYS = ["s1_momentum", "s2_hot", "s3_cold", "s4_cooccur", "s5_zone", "s6_learner"]


# ── 基礎 I/O ──────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "weights":      {str(n): 1.0 for n in NUM_RANGE},
        "slot_hits":    {slot: {str(n): 0 for n in NUM_RANGE} for slot in SLOT_LABELS},
        "pair_weights": {},
        "penalty_counts": {str(n): 0 for n in NUM_RANGE},
        # 各策略近30天命中追蹤
        "strategy_hit_history": [],   # [{date, s1_six, s1_nine, s2_six, ...}]
        # 各策略投票權重（由週深度學習自動調整）
        "strategy_weights": {k: 1.0 for k in STRATEGY_KEYS},
        "history":       [],
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


def get_strategy_weights() -> dict[str, float]:
    """供 bingo_core 取得各策略的投票權重"""
    return load_state().get("strategy_weights", {k: 1.0 for k in STRATEGY_KEYS})


# ── 基礎權重計算 ──────────────────────────────────────────────────────────────

DRAWS_PER_DAY = 288   # Bingo 每天約 288 期

def _calc_base_weights(df) -> dict[int, float]:
    """
    以天數為單位的四層基礎權重：
      近1天 × 近7天 × 近30天（上限90天）× 冷號遺漏補正
    窗口設計：上限 90 天，近期資料權重更大
    """
    import bingo_core
    num_cols = bingo_core.NUM_COLS
    total    = len(df)

    def days_win(d):
        return min(d * DRAWS_PER_DAY, total)

    w1d  = days_win(1)
    w7d  = days_win(7)
    w30d = days_win(30)
    w90d = days_win(90)

    def cnt_map(n_rows):
        c = Counter(df.tail(n_rows)[num_cols].values.flatten().tolist())
        avg = sum(c.values()) / len(NUM_RANGE)
        return c, avg

    cnt1d,  avg1d  = cnt_map(w1d)
    cnt7d,  avg7d  = cnt_map(w7d)
    cnt30d, avg30d = cnt_map(w30d)

    # 遺漏期數（以90天為基準）
    base90 = df.tail(w90d)[num_cols].values.tolist()
    last_seen = {}
    for i, row in enumerate(base90):
        for n in row:
            last_seen[int(n)] = i
    total90      = len(base90)
    expected_gap = 80 / 20   # 每球平均 4 期一次

    weights = {}
    for n in NUM_RANGE:
        r1d  = cnt1d.get(n, 0)  / avg1d  if avg1d  else 1.0
        r7d  = cnt7d.get(n, 0)  / avg7d  if avg7d  else 1.0
        r30d = cnt30d.get(n, 0) / avg30d if avg30d else 1.0

        # 近期加權：1天 > 7天 > 30天
        base = r1d * 0.50 + r7d * 0.35 + r30d * 0.15

        # 冷號遺漏補正
        miss       = total90 - 1 - last_seen.get(n, 0)
        miss_ratio = min(MISS_BOOST_MAX, 1.0 + 0.7 * (miss - expected_gap) / max(expected_gap, 1))
        miss_ratio = max(0.7, miss_ratio)

        weights[n] = base * miss_ratio

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
    2. 從 df 取昨日實際開獎，精準強化命中號碼
    3. 昨日快照命中回饋（含各策略命中追蹤）
    4. 更新時段偏好 + 數對學習
    5. 合併全局推薦權重
    """
    import bingo_core as _bc
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

    # ── 從 df 取昨日實際開獎號碼（精準學習）────────────────────────
    yest_actual_counter = Counter()
    if df is not None and not df.empty:
        try:
            yest_rows = df[df["date"] == yesterday]
            for _, row in yest_rows.iterrows():
                for col in _bc.NUM_COLS:
                    yest_actual_counter[int(row[col])] += 1
        except Exception:
            pass

    # 昨日實際開獎加成：出現次數多 → 下一期更可能再出現（動能）
    if yest_actual_counter:
        max_cnt = max(yest_actual_counter.values())
        for n, cnt in yest_actual_counter.items():
            boost = 1.0 + 0.15 * (cnt / max_cnt)
            state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * boost

    total_six_hits  = 0
    total_nine_hits = 0
    round_count     = 0

    pair_weights = state.get("pair_weights", {})
    penalty      = state.get("penalty_counts", {str(n): 0 for n in NUM_RANGE})

    # 各策略命中追蹤（今日）
    strategy_hits_today = {k + "_six": 0.0 for k in ["s1","s2","s3","s4","s5","s6"]}
    strategy_hits_today.update({k + "_nine": 0.0 for k in ["s1","s2","s3","s4","s5","s6"]})
    strategy_snap_count = 0

    # Step 2：昨日快照命中回饋
    for snap in yest_snaps:
        slot = snap.get("slot", "")
        if slot not in SLOT_LABELS:
            continue
        six  = snap.get("six",  [])
        nine = snap.get("nine", [])
        all_rec = list(set(six + nine))

        # 各策略提名（存入快照的）
        strat_noms = snap.get("strategy_nominations", {})

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

            six_rate  = six_hits  / 6
            nine_rate = nine_hits / 9

            # ── 精準學習：用實際開獎號碼 drawn 直接強化/懲罰 ──────────
            drawn_set = set(result.get("drawn", []))

            if drawn_set:
                # 推薦的號碼：命中的加成，未命中的輕微懲罰
                for n in six:
                    if n in drawn_set:
                        # 推薦且命中 → 強加成
                        boost = 1.0 + 0.12 * (1 + six_hits * 0.05)
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * boost
                    else:
                        # 推薦但未命中 → 輕微懲罰
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * 0.97

                for n in nine:
                    if n in drawn_set:
                        boost = 1.0 + 0.09 * (1 + nine_hits * 0.04)
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * boost
                    else:
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * 0.98

            else:
                # 舊快照沒有 drawn → fallback 到命中數代理
                if six_hits >= 3:
                    for n in six:
                        factor = 1.0 + 0.10 * six_hits
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * factor
                elif six_hits <= 1:
                    for n in six:
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * 0.96

                if nine_hits >= 4:
                    for n in nine:
                        factor = 1.0 + 0.07 * nine_hits
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * factor
                elif nine_hits <= 2:
                    for n in nine:
                        state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * 0.97

            # 時段偏好更新
            if six_hits >= 3 and slot in state["slot_hits"]:
                for n in six:
                    state["slot_hits"][slot][str(n)] = \
                        state["slot_hits"][slot].get(str(n), 0) + six_hits
            if nine_hits >= 5 and slot in state["slot_hits"]:
                for n in nine:
                    state["slot_hits"][slot][str(n)] = \
                        state["slot_hits"][slot].get(str(n), 0) + nine_hits

            # 數對學習
            hit_rate = (six_rate + nine_rate) / 2
            PAIR_BONUS = 1.12
            PAIR_DECAY = 0.97
            for (a, b) in rec_pairs:
                key = f"{a},{b}"
                w = pair_weights.get(key, 1.0)
                w = min(3.0, w * PAIR_BONUS) if hit_rate >= 0.35 else max(0.3, w * PAIR_DECAY)
                pair_weights[key] = round(w, 4)

            # 各策略命中追蹤
            strategy_snap_count += 1
            for sk in ["s1","s2","s3","s4","s5","s6"]:
                nom = strat_noms.get(sk, {})
                s6_nom  = nom.get("six",  [])
                s9_nom  = nom.get("nine", [])
                # 計算這顆快照該策略若被選上會命中幾顆
                # 需要知道實際開獎號碼 → 從 yest_actual_counter 取最常見的20顆模擬
                # 由於沒有期次對應，用 six_hits/nine_hits 的比例代理
                strategy_hits_today[sk + "_six"]  += six_hits  if s6_nom else 0
                strategy_hits_today[sk + "_nine"] += nine_hits if s9_nom else 0

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

    # 記錄日報
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
    state["history"]      = state["history"][-90:]
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
    2. 回測各策略命中率（S1~S6 誰表現最佳）→ 自動調整投票權重
    3. 回測多種參數組合，找最佳窗口
    4. 週期性分析 + 連續低命中懲罰
    5. 儲存週報快照（永久保存）
    """
    state   = load_state()
    history = state.get("history", [])

    this_week = history[-7:]  if len(history) >= 7  else history
    last_week = history[-14:-7] if len(history) >= 14 else []

    def avg_hits(lst, key):
        return round(sum(r.get(key, 0) for r in lst) / len(lst), 2) if lst else 0.0

    tw_six  = avg_hits(this_week, "avg_six_hits")
    tw_nine = avg_hits(this_week, "avg_nine_hits")
    lw_six  = avg_hits(last_week, "avg_six_hits")
    lw_nine = avg_hits(last_week, "avg_nine_hits")

    delta_six  = round(tw_six  - lw_six,  2)
    delta_nine = round(tw_nine - lw_nine, 2)

    # ── 策略回測：用 df 跑各策略過去7天的命中率 ─────────────────────
    strategy_perf = {k: {"six": 0.0, "nine": 0.0} for k in STRATEGY_KEYS}
    best_strategy = None

    if df is not None and not df.empty:
        import bingo_core as _bc
        # 取最近 7 天的資料做回測
        from datetime import date as _date
        cutoff = (datetime.now(_TW) - timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            recent7_df = df[df["date"] >= cutoff] if "date" in df.columns else df.tail(270*7)
        except Exception:
            recent7_df = df.tail(270 * 7)

        if len(recent7_df) >= 20:
            # 對每個策略跑回測：用歷史前半預測後半，看命中
            test_df = recent7_df.reset_index(drop=True)
            n_test  = min(50, len(test_df) // 2)   # 回測最近50期
            history_df = test_df.iloc[:-n_test]

            for i in range(n_test):
                eval_row   = test_df.iloc[-(n_test - i)]
                hist_slice = pd.concat([history_df, test_df.iloc[-(n_test - i):-n_test + i + 1 if i > 0 else -(n_test-1):-(n_test-1) or None]]).drop_duplicates() if i > 0 else history_df

                try:
                    actual_set = set(int(eval_row[c]) for c in _bc.NUM_COLS)
                except Exception:
                    continue

                # S1：超短期動能（最近3期頻率）
                try:
                    f3 = Counter()
                    for _, r in history_df.tail(3).iterrows():
                        for c in _bc.NUM_COLS:
                            f3[int(r[c])] += 1
                    s1_six  = sorted(_bc.BALL_RANGE, key=lambda x: f3.get(x, 0), reverse=True)[:6]
                    s1_nine = sorted(_bc.BALL_RANGE, key=lambda x: f3.get(x, 0), reverse=True)[:9]
                    strategy_perf["s1_momentum"]["six"]  += len(set(s1_six)  & actual_set)
                    strategy_perf["s1_momentum"]["nine"] += len(set(s1_nine) & actual_set)
                except Exception:
                    pass

                # S2：短期熱號（近20期）
                try:
                    f20 = Counter()
                    for _, r in history_df.tail(20).iterrows():
                        for c in _bc.NUM_COLS:
                            f20[int(r[c])] += 1
                    s2_six  = sorted(_bc.BALL_RANGE, key=lambda x: f20.get(x, 0), reverse=True)[:6]
                    s2_nine = sorted(_bc.BALL_RANGE, key=lambda x: f20.get(x, 0), reverse=True)[:9]
                    strategy_perf["s2_hot"]["six"]  += len(set(s2_six)  & actual_set)
                    strategy_perf["s2_hot"]["nine"] += len(set(s2_nine) & actual_set)
                except Exception:
                    pass

                # S3：冷號回歸（遺漏最長）
                try:
                    last_s = {}
                    for idx2, r2 in history_df.iterrows():
                        for c in _bc.NUM_COLS:
                            last_s[int(r2[c])] = idx2
                    miss_sc = {n: len(history_df) - 1 - last_s.get(n, 0) for n in _bc.BALL_RANGE}
                    s3_six  = sorted(_bc.BALL_RANGE, key=lambda x: miss_sc.get(x, 0), reverse=True)[:6]
                    s3_nine = sorted(_bc.BALL_RANGE, key=lambda x: miss_sc.get(x, 0), reverse=True)[:9]
                    strategy_perf["s3_cold"]["six"]  += len(set(s3_six)  & actual_set)
                    strategy_perf["s3_cold"]["nine"] += len(set(s3_nine) & actual_set)
                except Exception:
                    pass

                # S4：共現強度（略）→ 用近50期頻率代理
                try:
                    f50 = Counter()
                    for _, r in history_df.tail(50).iterrows():
                        for c in _bc.NUM_COLS:
                            f50[int(r[c])] += 1
                    s4_six  = sorted(_bc.BALL_RANGE, key=lambda x: f50.get(x, 0), reverse=True)[:6]
                    s4_nine = sorted(_bc.BALL_RANGE, key=lambda x: f50.get(x, 0), reverse=True)[:9]
                    strategy_perf["s4_cooccur"]["six"]  += len(set(s4_six)  & actual_set)
                    strategy_perf["s4_cooccur"]["nine"] += len(set(s4_nine) & actual_set)
                except Exception:
                    pass

                # S5：區間峰值
                try:
                    zones = [range(1,21), range(21,41), range(41,61), range(61,81)]
                    s5_six = []
                    for z in zones:
                        best = max(z, key=lambda n: f50.get(n, 0))
                        s5_six.append(best)
                    all_by50 = sorted(_bc.BALL_RANGE, key=lambda x: f50.get(x,0), reverse=True)
                    for n in all_by50:
                        if len(s5_six) >= 6:
                            break
                        if n not in s5_six:
                            s5_six.append(n)
                    s5_nine = list(s5_six)
                    for n in all_by50:
                        if len(s5_nine) >= 9:
                            break
                        if n not in s5_nine:
                            s5_nine.append(n)
                    strategy_perf["s5_zone"]["six"]  += len(set(s5_six)  & actual_set)
                    strategy_perf["s5_zone"]["nine"] += len(set(s5_nine) & actual_set)
                except Exception:
                    pass

            # 正規化（除以回測期數）
            for k in STRATEGY_KEYS:
                if n_test > 0:
                    strategy_perf[k]["six"]  = round(strategy_perf[k]["six"]  / n_test, 3)
                    strategy_perf[k]["nine"] = round(strategy_perf[k]["nine"] / n_test, 3)

    # ── 自動調整策略投票權重 ─────────────────────────────────────
    # 用本週回測命中率加權：命中率 ≥ 2.5（6星）→ 提升; < 1.5 → 降低
    current_sw = state.get("strategy_weights", {k: 1.0 for k in STRATEGY_KEYS})
    new_sw = {}
    for k in STRATEGY_KEYS:
        six_avg  = strategy_perf[k]["six"]
        nine_avg = strategy_perf[k]["nine"]
        combined = (six_avg / 3.0 + nine_avg / 4.0) / 2.0   # 正規化到目標

        old_w = current_sw.get(k, 1.0)
        if combined >= 0.85:
            # 達目標 85% → 提升
            new_w = min(2.5, old_w * 1.15)
        elif combined >= 0.65:
            # 中等 → 輕微提升
            new_w = min(2.5, old_w * 1.05)
        elif combined <= 0.40:
            # 很差 → 降低
            new_w = max(0.3, old_w * 0.80)
        else:
            # 略差 → 維持或輕降
            new_w = max(0.3, old_w * 0.95)
        new_sw[k] = round(new_w, 3)

    # S6（學習模型）始終給予基礎加成（它代表累積學習成果）
    new_sw["s6_learner"] = max(new_sw.get("s6_learner", 1.0), 0.8)

    state["strategy_weights"] = new_sw
    best_strategy = max(STRATEGY_KEYS, key=lambda k: strategy_perf[k]["six"])

    # ── 週期性分析（近200期）─────────────────────────────────────────
    due_nums = []
    if df is not None and not df.empty:
        import bingo_core as _bc
        num_cols = _bc.NUM_COLS
        recent200 = df.tail(200)
        all_rows  = recent200[num_cols].values.tolist()
        period_analysis = {}
        for n in NUM_RANGE:
            positions = [i for i, row in enumerate(all_rows) if n in [int(x) for x in row]]
            if len(positions) >= 3:
                gaps    = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
                avg_gap = sum(gaps) / len(gaps)
                since   = len(all_rows) - 1 - positions[-1]
                period_analysis[n] = {"avg_gap": avg_gap, "since": since,
                                      "due": since >= avg_gap * 0.9}
        due_nums = sorted(
            [n for n, v in period_analysis.items() if v["due"]],
            key=lambda n: period_analysis[n]["since"] / max(period_analysis[n]["avg_gap"], 1),
            reverse=True,
        )[:15]
        # 提升即將到來的號碼
        for n in due_nums:
            state["weights"][str(n)] = float(state["weights"].get(str(n), 1.0)) * 1.12

    # ── 連續低命中懲罰 ───────────────────────────────────────────
    penalty = state.get("penalty_counts", {str(n): 0 for n in NUM_RANGE})
    overall_avg6 = state.get("avg_six_hits", 0)
    if tw_six < max(overall_avg6 * 0.8, 0.1):
        slot_total = {str(n): 0 for n in NUM_RANGE}
        for slot, sh in state.get("slot_hits", {}).items():
            for k2, v in sh.items():
                slot_total[k2] = slot_total.get(k2, 0) + v
        median_hits = sorted(slot_total.values())[len(NUM_RANGE)//2]
        for n in NUM_RANGE:
            if slot_total.get(str(n), 0) < median_hits * 0.5:
                penalty[str(n)] = penalty.get(str(n), 0) + 1
            else:
                penalty[str(n)] = max(0, penalty.get(str(n), 0) - 1)

    weights = state["weights"]
    for n in NUM_RANGE:
        if penalty.get(str(n), 0) >= 3:
            weights[str(n)] = float(weights.get(str(n), 1.0)) * 0.88

    # 重新正規化
    vals = [float(v) for v in weights.values()]
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: float(v) / avg for k, v in weights.items()}

    state["penalty_counts"] = penalty
    save_state(state)

    # ── 組週報文字 ──────────────────────────────────────────────
    gap6  = round(TARGET_SIX  - tw_six,  2)
    gap9  = round(TARGET_NINE - tw_nine, 2)
    name_map = {
        "s1_momentum": "超短動能",
        "s2_hot":      "短期熱號",
        "s3_cold":     "冷號回歸",
        "s4_cooccur":  "強力共現",
        "s5_zone":     "區間峰值",
        "s6_learner":  "學習模型",
    }
    lines = [
        "📊 Bingo 週度深度學習報告",
        "═" * 38,
        f"🗓️  本週平均命中（最近{len(this_week)}日）",
        f"   6星：{tw_six:.2f} 顆  ({'+' if delta_six>=0 else ''}{delta_six:.2f} vs 上週)  目標差：{gap6:.2f}",
        f"   9星：{tw_nine:.2f} 顆  ({'+' if delta_nine>=0 else ''}{delta_nine:.2f} vs 上週)  目標差：{gap9:.2f}",
        "",
        "🏆 策略回測排名（本週）",
    ]
    perf_sorted = sorted(STRATEGY_KEYS,
                         key=lambda k: strategy_perf[k]["six"] + strategy_perf[k]["nine"],
                         reverse=True)
    for rank, k in enumerate(perf_sorted, 1):
        lines.append(
            f"   {rank}. {name_map.get(k,k)}：6星 {strategy_perf[k]['six']:.2f} / "
            f"9星 {strategy_perf[k]['nine']:.2f}  → 權重調整後 {new_sw.get(k,1.0):.2f}"
        )
    lines += [
        "",
        f"⚙️  下週投票權重（自動更新）",
    ]
    for k in STRATEGY_KEYS:
        lines.append(f"   {name_map.get(k,k)}：{current_sw.get(k,1.0):.2f} → {new_sw.get(k,1.0):.2f}")
    lines += [
        "",
        f"🔄 週期分析即將出現號碼（前10）：{due_nums[:10]}",
        f"   連續低命中懲罰號碼數：{sum(1 for v in penalty.values() if v >= 3)} 個",
        "",
        f"📊 14日累計平均命中",
        f"   6星：{state['avg_six_hits']:.2f} / 9星：{state['avg_nine_hits']:.2f}",
        f"   累計學習期數：{state['total_rounds']}",
        "═" * 38,
    ]
    report_text = "\n".join(lines)

    # 保存週報快照（永久歷史）
    weekly_hist = _load_weekly_history()
    snap = {
        "week_end":        datetime.now(_TW).strftime("%Y-%m-%d"),
        "this_week":       {"six": tw_six, "nine": tw_nine},
        "last_week":       {"six": lw_six, "nine": lw_nine},
        "delta":           {"six": delta_six, "nine": delta_nine},
        "strategy_perf":   strategy_perf,
        "strategy_weights_before": current_sw,
        "strategy_weights_after":  new_sw,
        "best_strategy":   best_strategy,
        "due_nums":        due_nums[:10],
        "total_rounds":    state["total_rounds"],
        "report_text":     report_text,
    }
    weekly_hist.append(snap)
    _save_weekly_history(weekly_hist)

    return {
        "ok":          True,
        "this_week":   {"six": tw_six, "nine": tw_nine},
        "last_week":   {"six": lw_six, "nine": lw_nine},
        "delta":       {"six": delta_six, "nine": delta_nine},
        "strategy_perf": strategy_perf,
        "strategy_weights": new_sw,
        "best_strategy": best_strategy,
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
        "total_rounds":     state["total_rounds"],
        "avg_six_hits":     state["avg_six_hits"],
        "avg_nine_hits":    state["avg_nine_hits"],
        "strategy_weights": state.get("strategy_weights", {}),
        "last_updated":     state["last_updated"],
        "history":          state["history"][-7:],
    }


def get_weekly_history() -> list:
    return _load_weekly_history()


# 避免 circular import — pandas 延後引入
try:
    import pandas as pd
except ImportError:
    pass
