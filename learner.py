"""
演算法學習模組（三策略獨立學習 + 整合投票權重）
─────────────────────────────────────────────────────────
每次開獎後：
  1. 比對三組推薦（A機率/B價值/C策略）vs 實際開獎，記錄各策略命中數
  2. 根據命中更新學習權重（全策略共用號碼權重）
  3. 追蹤三策略近30期平均命中，勝出策略自動加重整合投票比重
  4. 每週一提供深度分析報告

目標：透過學習讓平均命中數逼近 3 顆以上
"""

import json
from pathlib import Path
from datetime import datetime

import pandas as pd

_DATA_DIR  = Path("/data") if Path("/data").exists() else Path(__file__).parent
LEARN_FILE = _DATA_DIR / "learn_state.json"
BALL_RANGE  = list(range(1, 40))

# 衰減因子
DECAY        = 0.90
# 命中加成
HIT_BONUS    = 2.5
# 遺漏懲罰
MISS_PENALTY = 0.75


def _default_state() -> dict:
    return {
        "weights": {str(n): 1.0 for n in BALL_RANGE},
        "history": [],          # [{date, actual, prob_rec, value_rec, strategy_rec, prob_hits, value_hits, strategy_hits}]
        "last_checked":      None,
        "total_rounds":      0,
        # 三策略近30期平均命中
        "prob_avg_hits":     0.0,
        "value_avg_hits":    0.0,
        "strategy_avg_hits": 0.0,
        # 整合投票權重（根據近30期勝率自動更新）
        "ensemble_scores": {"prob": 1.0, "value": 1.0, "strategy": 1.0},
    }


def load_state() -> dict:
    if LEARN_FILE.exists():
        try:
            s = json.loads(LEARN_FILE.read_text())
            # 補齊舊版缺少的欄位
            for k, v in _default_state().items():
                if k not in s:
                    s[k] = v
            return s
        except Exception:
            pass
    return _default_state()


def save_state(state: dict):
    LEARN_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_weights() -> dict[int, float]:
    state = load_state()
    return {int(k): v for k, v in state["weights"].items()}


def get_ensemble_scores() -> dict:
    return load_state().get("ensemble_scores", {"prob": 1.0, "value": 1.0, "strategy": 1.0})


def update(actual_nums: list[int],
           prob_rec: list[int],
           value_rec: list[int],
           strategy_rec: list[int] | None = None) -> tuple[int, int, int]:
    """傳入三組推薦 + 實際開獎號碼，更新學習狀態。"""
    state = load_state()

    strategy_rec = strategy_rec or []
    prob_hits     = len(set(prob_rec)     & set(actual_nums))
    value_hits    = len(set(value_rec)    & set(actual_nums))
    strategy_hits = len(set(strategy_rec) & set(actual_nums))

    # 衰減所有現有權重
    for k in state["weights"]:
        state["weights"][k] *= DECAY

    # 命中號碼加分（三策略都命中 → 加分更多）
    for n in actual_nums:
        key = str(n)
        if key in state["weights"]:
            bonus = HIT_BONUS
            # 若多策略都命中此號碼，額外加成
            hits_count = sum(1 for rec in [prob_rec, value_rec, strategy_rec] if n in rec)
            bonus += hits_count * 0.3
            state["weights"][key] *= bonus

    # 遺漏號碼輕微懲罰
    missed = set(BALL_RANGE) - set(actual_nums)
    for n in missed:
        key = str(n)
        if key in state["weights"]:
            state["weights"][key] *= MISS_PENALTY

    # 正規化（保持平均為 1.0）
    vals = list(state["weights"].values())
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: v / avg for k, v in state["weights"].items()}

    # 記錄歷史
    record = {
        "date":          datetime.now().strftime("%Y-%m-%d"),
        "actual":        actual_nums,
        "prob_rec":      prob_rec,
        "value_rec":     value_rec,
        "strategy_rec":  strategy_rec,
        "prob_hits":     prob_hits,
        "value_hits":    value_hits,
        "strategy_hits": strategy_hits,
    }
    state["history"].append(record)
    state["history"] = state["history"][-180:]  # 保留最近180筆（約半年）
    state["last_checked"] = record["date"]
    state["total_rounds"] += 1

    # 更新三策略近30期平均命中
    recent30 = state["history"][-30:]
    state["prob_avg_hits"]     = round(sum(r["prob_hits"]     for r in recent30) / len(recent30), 2)
    state["value_avg_hits"]    = round(sum(r["value_hits"]    for r in recent30) / len(recent30), 2)
    state["strategy_avg_hits"] = round(sum(r["strategy_hits"] for r in recent30) / len(recent30), 2)

    # 更新整合投票權重（近30期命中率 → 正規化為投票比重）
    avgs = {
        "prob":     max(state["prob_avg_hits"],     0.1),
        "value":    max(state["value_avg_hits"],    0.1),
        "strategy": max(state["strategy_avg_hits"], 0.1),
    }
    total_score = sum(avgs.values())
    state["ensemble_scores"] = {k: round(v / total_score * 3, 3) for k, v in avgs.items()}

    save_state(state)
    return prob_hits, value_hits, strategy_hits


def auto_update_from_df(df: pd.DataFrame,
                        last_prob: list[int],
                        last_value: list[int],
                        last_strategy: list[int] | None = None) -> dict | None:
    """比對資料最新一期是否已對獎，若尚未對獎則執行 update()。"""
    state = load_state()
    latest = df.iloc[-1]
    latest_date = latest["date"].strftime("%Y-%m-%d")

    if state["last_checked"] == latest_date:
        return None
    if not last_prob or not last_value:
        return None

    actual = [int(latest[c]) for c in ["n1", "n2", "n3", "n4", "n5"]]
    prob_hits, value_hits, strategy_hits = update(actual, last_prob, last_value, last_strategy or [])

    state = load_state()
    return {
        "date":           latest_date,
        "actual":         actual,
        "prob_hits":      prob_hits,
        "value_hits":     value_hits,
        "strategy_hits":  strategy_hits,
        "ensemble_scores": state.get("ensemble_scores", {}),
    }


def weekly_deep_analysis() -> dict:
    """
    每週一執行深度分析：
    - 計算本週（最近7筆）vs 上週（前7筆）命中率變化
    - 分析哪個策略最近表現最好
    - 輸出改進建議文字
    """
    state = load_state()
    history = state.get("history", [])
    if len(history) < 2:
        return {"ok": False, "reason": "資料不足，至少需要2筆歷史"}

    recent7 = history[-7:]
    prev7   = history[-14:-7] if len(history) >= 14 else history[:-7]

    def avg(lst, key):
        return round(sum(r.get(key, 0) for r in lst) / len(lst), 2) if lst else 0.0

    this_week = {
        "prob":     avg(recent7, "prob_hits"),
        "value":    avg(recent7, "value_hits"),
        "strategy": avg(recent7, "strategy_hits"),
        "rounds":   len(recent7),
    }
    last_week = {
        "prob":     avg(prev7, "prob_hits"),
        "value":    avg(prev7, "value_hits"),
        "strategy": avg(prev7, "strategy_hits"),
        "rounds":   len(prev7),
    }

    # 最佳策略
    best_key  = max(this_week, key=lambda k: this_week[k] if k != "rounds" else -1)
    best_val  = this_week[best_key]

    # 本週最高命中單期
    best_round = max(history[-7:], key=lambda r: r.get("prob_hits", 0) + r.get("value_hits", 0) + r.get("strategy_hits", 0), default={})

    # 最近30期平均
    r30 = history[-30:]
    avg30 = {
        "prob":     avg(r30, "prob_hits"),
        "value":    avg(r30, "value_hits"),
        "strategy": avg(r30, "strategy_hits"),
    }

    # 週變化
    delta = {
        "prob":     round(this_week["prob"]     - last_week["prob"],     2),
        "value":    round(this_week["value"]    - last_week["value"],    2),
        "strategy": round(this_week["strategy"] - last_week["strategy"], 2),
    }

    name_map = {"prob": "機率推薦", "value": "價值推薦", "strategy": "策略推薦"}
    lines = [
        f"📈 539 週度學習報告",
        f"═" * 34,
        f"🗓️  本週（最近{len(recent7)}期）命中分析",
        f"   機率推薦  ：平均 {this_week['prob']:.2f} 顆  ({'+' if delta['prob']>=0 else ''}{delta['prob']:.2f} vs 上週)",
        f"   價值推薦  ：平均 {this_week['value']:.2f} 顆  ({'+' if delta['value']>=0 else ''}{delta['value']:.2f} vs 上週)",
        f"   策略推薦  ：平均 {this_week['strategy']:.2f} 顆  ({'+' if delta['strategy']>=0 else ''}{delta['strategy']:.2f} vs 上週)",
        f"",
        f"🏆 本週最佳策略：{name_map[best_key]}（平均 {best_val:.2f} 顆）",
        f"",
        f"📊 累計30期平均命中：",
        f"   機率:{avg30['prob']:.2f} / 價值:{avg30['value']:.2f} / 策略:{avg30['strategy']:.2f}",
        f"   目標：3.0 顆 | 差距：{round(3.0 - max(avg30.values()), 2)} 顆",
        f"",
        f"⚙️  目前整合投票權重（自動調整中）：",
    ]
    for k, v in state.get("ensemble_scores", {}).items():
        lines.append(f"   {name_map.get(k, k)}：{v:.3f}")

    lines += [
        f"",
        f"📝 總學習期數：{state['total_rounds']} 期",
        f"═" * 34,
    ]

    return {
        "ok":        True,
        "this_week": this_week,
        "last_week": last_week,
        "delta":     delta,
        "avg30":     avg30,
        "best_strategy": best_key,
        "total_rounds":  state["total_rounds"],
        "ensemble_scores": state.get("ensemble_scores", {}),
        "report_text": "\n".join(lines),
    }


def get_summary() -> dict:
    state = load_state()
    return {
        "total_rounds":      state["total_rounds"],
        "prob_avg_hits":     state["prob_avg_hits"],
        "value_avg_hits":    state["value_avg_hits"],
        "strategy_avg_hits": state["strategy_avg_hits"],
        "ensemble_scores":   state.get("ensemble_scores", {}),
        "last_checked":      state["last_checked"],
        "history":           state["history"][-10:],
    }
