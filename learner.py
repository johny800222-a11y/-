"""
演算法學習模組
─────────────────────────────────────────────────────────
每次開獎後：
  1. 對照上期推薦 vs 實際開獎，計算命中數
  2. 更新各號碼的「學習權重」（命中加分、遺漏扣分）
  3. 記錄歷史命中率，供前端圖表顯示

學習權重會疊加在頻率統計上，使近期表現好的號碼獲得更高權重。
"""

import json
from pathlib import Path
from datetime import datetime

import pandas as pd

_DATA_DIR  = Path("/data") if Path("/data").exists() else Path(__file__).parent
LEARN_FILE = _DATA_DIR / "learn_state.json"
BALL_RANGE  = list(range(1, 40))

# 衰減因子：舊的推薦對權重的影響隨時間遞減
DECAY       = 0.85
# 命中加成倍率
HIT_BONUS   = 2.0
# 遺漏懲罰倍率
MISS_PENALTY = 0.6


def _default_state() -> dict:
    return {
        "weights":           {str(n): 1.0 for n in BALL_RANGE},
        "history":           [],   # [{date, prob_hits, value_hits, strategy_hits, actual}]
        "last_checked":      None,
        "total_rounds":      0,
        "prob_avg_hits":     0.0,
        "value_avg_hits":    0.0,
        "strategy_avg_hits": 0.0,  # 策略推薦近30期平均命中
    }


def load_state() -> dict:
    if LEARN_FILE.exists():
        try:
            return json.loads(LEARN_FILE.read_text())
        except Exception:
            pass
    return _default_state()


def save_state(state: dict):
    LEARN_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_weights() -> dict[int, float]:
    """回傳 {號碼: 學習權重}，供推薦模組使用"""
    state = load_state()
    return {int(k): v for k, v in state["weights"].items()}


def update(actual_nums: list[int], prob_rec: list[int], value_rec: list[int]):
    """
    傳入本期實際開獎號碼、上期的兩組推薦，更新學習狀態。
    """
    state = load_state()

    prob_hits  = len(set(prob_rec)  & set(actual_nums))
    value_hits = len(set(value_rec) & set(actual_nums))

    # 衰減所有現有權重
    for k in state["weights"]:
        state["weights"][k] *= DECAY

    # 命中號碼加分
    for n in actual_nums:
        key = str(n)
        if key in state["weights"]:
            state["weights"][key] *= HIT_BONUS

    # 未開出號碼輕微懲罰（避免過度集中）
    missed = set(BALL_RANGE) - set(actual_nums)
    for n in missed:
        key = str(n)
        if key in state["weights"]:
            state["weights"][key] *= MISS_PENALTY

    # 正規化：保持平均為 1.0
    vals = list(state["weights"].values())
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: v / avg for k, v in state["weights"].items()}

    # 記錄歷史
    record = {
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "actual":      actual_nums,
        "prob_rec":    prob_rec,
        "value_rec":   value_rec,
        "prob_hits":   prob_hits,
        "value_hits":  value_hits,
    }
    state["history"].append(record)
    state["history"] = state["history"][-90:]  # 保留最近90筆
    state["last_checked"] = record["date"]
    state["total_rounds"] += 1

    # 滾動平均命中數
    recent = state["history"][-30:]
    state["prob_avg_hits"]  = round(sum(r["prob_hits"]  for r in recent) / len(recent), 2)
    state["value_avg_hits"] = round(sum(r["value_hits"] for r in recent) / len(recent), 2)

    save_state(state)
    return prob_hits, value_hits


def auto_update_from_df(df: pd.DataFrame, last_prob: list[int], last_value: list[int],
                        last_strategy: list[int] = None) -> dict | None:
    """
    比對資料中最新一期是否已對獎，若尚未對獎則執行 update()。
    同時追蹤策略推薦命中數。
    """
    state = load_state()
    latest = df.iloc[-1]
    latest_date = latest["date"].strftime("%Y-%m-%d")

    if state["last_checked"] == latest_date:
        return None
    if not last_prob or not last_value:
        return None

    actual = [int(latest[c]) for c in ["n1", "n2", "n3", "n4", "n5"]]
    prob_hits, value_hits = update(actual, last_prob, last_value)

    # 策略推薦命中追蹤（不影響學習權重，只統計）
    strategy_hits = len(set(last_strategy) & set(actual)) if last_strategy else 0
    state = load_state()  # 重新讀（update 已存檔）
    history = state.get("history", [])
    if history:
        history[-1]["strategy_hits"] = strategy_hits
    recent = history[-30:]
    state["strategy_avg_hits"] = round(
        sum(r.get("strategy_hits", 0) for r in recent) / len(recent), 2
    ) if recent else 0.0
    # 補齊舊記錄缺少的 strategy_avg_hits 欄位
    if "strategy_avg_hits" not in state:
        state["strategy_avg_hits"] = 0.0
    save_state(state)

    return {
        "date":           latest_date,
        "actual":         actual,
        "prob_hits":      prob_hits,
        "value_hits":     value_hits,
        "strategy_hits":  strategy_hits,
    }


def get_summary() -> dict:
    state = load_state()
    # 相容舊版（缺少 strategy_avg_hits）
    if "strategy_avg_hits" not in state:
        state["strategy_avg_hits"] = 0.0
    return {
        "total_rounds":      state["total_rounds"],
        "prob_avg_hits":     state["prob_avg_hits"],
        "value_avg_hits":    state["value_avg_hits"],
        "strategy_avg_hits": state["strategy_avg_hits"],
        "last_checked":      state["last_checked"],
        "history":         state["history"][-10:],  # 最近10筆給前端
    }
