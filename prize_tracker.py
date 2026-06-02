"""
539 中獎人數追蹤模組
──────────────────────────────────────────────
從台彩官方 API 抓取每期真實中獎人數，
分析哪些號碼組合開出時，中獎人數少（台彩獲利較高）。

資料來源：api.taiwanlottery.com/TLCAPIWeB/Lottery/Daily539Result
每日 22:00 更新一次，傳送分析報告到 Telegram。
"""

import json
import ssl
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import pytz

_TW = pytz.timezone("Asia/Taipei")
_DATA_DIR = Path("/data") if Path("/data").exists() else Path(__file__).parent
PRIZE_FILE = _DATA_DIR / "prize_tracker.json"

API_URL = "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/Daily539Result"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
    "Referer": "https://www.taiwanlottery.com/",
}

NUM_RANGE = list(range(1, 40))

# ── 獨得率最大化：避開人氣號碼的乘數 ─────────────────────────────────────────
# 原理：539 頭獎是獎池制，中獎人越少獨得機率越高。
# 玩家傾向選「生日號」(1-31) 與「文化幸運數」(6/8/9)，這些號碼超買，
# 即使開出也常多人分獎。32-39 幾乎無生日對應，人少買，獨得率最高。
_SOLO_WIN_MULTIPLIER: dict[int, float] = {
    # 文化幸運數（最多人選，懲罰最重）
    6: 0.58,  8: 0.53,  9: 0.60,
    # 月份數 1-12（生日月份偏好）
    1: 0.68,  2: 0.70,  3: 0.71,  4: 0.72,  5: 0.72,
    7: 0.73, 10: 0.74, 11: 0.75, 12: 0.74,
    # 日期數 13-31（仍在生日日期範圍）
    13: 0.82, 14: 0.83, 15: 0.83, 16: 0.84, 17: 0.84,
    18: 0.84, 19: 0.85, 20: 0.84, 21: 0.85, 22: 0.84,
    23: 0.85, 24: 0.85, 25: 0.84, 26: 0.85, 27: 0.85,
    28: 0.85, 29: 0.86, 30: 0.86, 31: 0.87,
    # 32-39：非生日對應區，人最少選 → 獨得率加成
    32: 1.15, 33: 1.16, 34: 1.18, 35: 1.13,
    36: 1.18, 37: 1.20, 38: 1.18, 39: 1.22,
}


def _fix_consecutive(picked: list[int], scores: dict[int, float]) -> list[int]:
    """
    若最終5碼中有3顆以上連號，把連號段中分數最低的換掉，
    以避免「連號組合」（這是另一類人氣型選法）。
    最多嘗試3輪。
    """
    result = list(picked)
    for _ in range(3):
        nums = sorted(result)
        # 找出所有長度 ≥ 3 的連號段
        long_run = []
        cur = [nums[0]]
        for i in range(1, len(nums)):
            if nums[i] == nums[i - 1] + 1:
                cur.append(nums[i])
            else:
                if len(cur) >= 3:
                    long_run.extend(cur)
                cur = [nums[i]]
        if len(cur) >= 3:
            long_run.extend(cur)

        if not long_run:
            break

        # 換掉連號段中分數最低的
        weakest = min(long_run, key=lambda n: scores.get(n, 0))
        result.remove(weakest)
        # 找分數最高的非連號替補
        result_set = set(result)
        for n in sorted(NUM_RANGE, key=lambda n: -scores.get(n, 0)):
            if n in result_set:
                continue
            # 確保加入後不產生新的長連號
            test = sorted(result_set | {n})
            new_run = False
            run_len = 1
            for i in range(1, len(test)):
                if test[i] == test[i - 1] + 1:
                    run_len += 1
                    if run_len >= 3:
                        new_run = True
                        break
                else:
                    run_len = 1
            if not new_run:
                result = sorted(list(result_set) + [n])
                break
        else:
            result = sorted(list(result_set) + [weakest])  # 找不到替換就放回

    return sorted(result)


# ── I/O ───────────────────────────────────────────────────────────────────────

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def load_prize_data() -> dict:
    if PRIZE_FILE.exists():
        try:
            return json.loads(PRIZE_FILE.read_text())
        except Exception:
            pass
    return {"records": [], "last_fetched": None}


def save_prize_data(data: dict):
    PRIZE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── 抓取台彩 API ──────────────────────────────────────────────────────────────

def fetch_prize_records(month: str = None, page_size: int = 30) -> list[dict]:
    """
    抓取指定月份（預設當月）的各期中獎人數。
    回傳 list of {
        period, date, numbers,
        sell_amount, total_prize,
        jackpot_count, jackpot_prize,
        second_count, second_prize,
        third_count, third_prize,
        fourth_count, fourth_prize,
    }
    """
    if month is None:
        month = datetime.now(_TW).strftime("%Y-%m")

    url = f"{API_URL}?month={month}&pageNum=1&pageSize={page_size}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return []

    if data.get("rtCode") != 0:
        return []

    records = []
    for item in data.get("content", {}).get("daily539Res", []):
        records.append({
            "period":       item["period"],
            "date":         item["lotteryDate"][:10],
            "numbers":      sorted(item["drawNumberSize"]),
            "sell_amount":  item.get("sellAmount", 0),
            "total_prize":  item.get("totalAmount", 0),
            # 各獎
            "jackpot_count":  item.get("d539JackpotAssign", {}).get("winnerCount", 0),
            "jackpot_prize":  item.get("d539JackpotAssign", {}).get("perPrize", 8000000),
            "second_count":   item.get("d539SecondAssign",  {}).get("winnerCount", 0),
            "second_prize":   item.get("d539SecondAssign",  {}).get("perPrize", 20000),
            "third_count":    item.get("d539ThirdAssign",   {}).get("winnerCount", 0),
            "third_prize":    item.get("d539ThirdAssign",   {}).get("perPrize", 300),
            "fourth_count":   item.get("d539FourthAssign",  {}).get("winnerCount", 0),
            "fourth_prize":   item.get("d539FourthAssign",  {}).get("perPrize", 50),
        })
    return records


def update_prize_data(months_back: int = 2) -> dict:
    """更新中獎人數資料，預設抓近 2 個月，首次執行自動補抓 12 個月"""
    now = datetime.now(_TW)
    existing = load_prize_data()
    existing_periods = {r["period"] for r in existing["records"]}

    # 若資料不足 100 筆，自動補抓 12 個月歷史
    if len(existing["records"]) < 100:
        months_back = 12

    months = []
    for i in range(months_back):
        d = now.replace(day=1) - timedelta(days=i * 28)
        months.append(d.strftime("%Y-%m"))

    new_records = []
    for month in months:
        for rec in fetch_prize_records(month, page_size=60):
            if rec["period"] not in existing_periods:
                new_records.append(rec)
                existing_periods.add(rec["period"])

    existing["records"].extend(new_records)
    existing["records"] = sorted(existing["records"], key=lambda r: r["period"])[-500:]
    existing["last_fetched"] = now.strftime("%Y-%m-%d %H:%M")
    save_prize_data(existing)
    return {"added": len(new_records), "total": len(existing["records"])}


# ── 分析：哪些號碼開出時，中獎人數少 ──────────────────────────────────────────

def analyze_low_winner_numbers(window: int = 100) -> dict:
    """
    統計每個號碼（1-39）出現時，各獎中獎人數的平均值。
    越低 = 該號碼出現時，中的人越少 = 台彩獲利越高。
    """
    data = load_prize_data()
    records = data["records"][-window:]
    if not records:
        return {}

    # 每個號碼的中獎人數累計
    num_stats = defaultdict(lambda: {
        "count": 0,            # 出現期數
        "jackpot_total": 0,
        "second_total": 0,
        "third_total": 0,
        "fourth_total": 0,
        "sell_total": 0,
        "prize_total": 0,
    })

    for rec in records:
        for n in rec["numbers"]:
            s = num_stats[n]
            s["count"] += 1
            s["jackpot_total"] += rec["jackpot_count"]
            s["second_total"]  += rec["second_count"]
            s["third_total"]   += rec["third_count"]
            s["fourth_total"]  += rec["fourth_count"]
            s["sell_total"]    += rec["sell_amount"]
            s["prize_total"]   += rec["total_prize"]

    result = {}
    for n in NUM_RANGE:
        s = num_stats[n]
        cnt = s["count"] or 1
        result[n] = {
            "appear_count":    s["count"],
            "avg_jackpot":     round(s["jackpot_total"] / cnt, 2),
            "avg_second":      round(s["second_total"]  / cnt, 1),
            "avg_third":       round(s["third_total"]   / cnt, 1),
            "avg_fourth":      round(s["fourth_total"]  / cnt, 1),
            # 台彩獲利率（銷售 - 獎金）/ 銷售
            "avg_profit_rate": round((s["sell_total"] - s["prize_total"]) / max(s["sell_total"], 1) * 100, 1),
        }

    return result


def find_low_winner_combinations(top_n: int = 5) -> dict:
    """
    找出「有人中獎但不多」的最佳號碼組合。
    策略：頭獎=0~2注 且 二獎最少 的期別，分析其號碼特徵。
    """
    data = load_prize_data()
    records = data["records"]

    # 篩選「甜蜜點」期別：頭獎 0~2 注，二獎 < 150 注
    sweet_spot = [
        r for r in records
        if 0 <= r["jackpot_count"] <= 2 and r["second_count"] < 150
    ]

    # 這些期別的號碼出現頻率
    num_freq = defaultdict(int)
    for rec in sweet_spot:
        for n in rec["numbers"]:
            num_freq[n] += 1

    total = len(sweet_spot) or 1
    freq_pct = {n: round(num_freq[n] / total * 100, 1) for n in NUM_RANGE}

    # 按出現率排序，最高的是甜蜜點常見號碼
    ranked = sorted(freq_pct.items(), key=lambda x: -x[1])

    # 號碼分析
    num_analysis = analyze_low_winner_numbers()

    return {
        "sweet_spot_periods": len(sweet_spot),
        "total_periods": len(records),
        "top_numbers": ranked[:15],        # 甜蜜點最常見號碼
        "low_winner_numbers": sorted(     # 出現時中獎人數最少的號碼
            NUM_RANGE,
            key=lambda n: num_analysis.get(n, {}).get("avg_second", 999)
        )[:10],
        "high_profit_numbers": sorted(   # 出現時台彩獲利率最高的號碼
            NUM_RANGE,
            key=lambda n: -num_analysis.get(n, {}).get("avg_profit_rate", 0)
        )[:10],
        "num_analysis": num_analysis,
    }


# ── Faker 策略 ───────────────────────────────────────────────────────────────

FAKER_FILE        = _DATA_DIR / "faker_tracker.json"
FAKER_LEARN_FILE  = _DATA_DIR / "faker_learn.json"
FAKER_WEEKLY_FILE = _DATA_DIR / "faker_learn_weekly.json"

# 學習目標
TARGET_HITS = 5.0


# ── Faker 學習狀態 I/O ────────────────────────────────────────────────────────

def _default_faker_learn() -> dict:
    return {
        "weights":          {str(n): 1.0 for n in NUM_RANGE},   # 號碼學習權重
        "history":          [],    # 每日記錄 [{date, picks, actual, hits, hits_3plus}]
        "total_rounds":     0,
        "avg_hits":         0.0,
        "hits_3plus_rate":  0.0,   # 近30期 ≥3顆命中率（%），主要獲利指標
        "last_updated":     None,
    }


def _load_faker_learn() -> dict:
    if FAKER_LEARN_FILE.exists():
        try:
            s = json.loads(FAKER_LEARN_FILE.read_text())
            for k, v in _default_faker_learn().items():
                if k not in s:
                    s[k] = v
            return s
        except Exception:
            pass
    return _default_faker_learn()


def _save_faker_learn(state: dict):
    FAKER_LEARN_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _load_faker_weekly() -> list:
    if FAKER_WEEKLY_FILE.exists():
        try:
            return json.loads(FAKER_WEEKLY_FILE.read_text())
        except Exception:
            pass
    return []


def _save_faker_weekly(data: list):
    FAKER_WEEKLY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def get_faker_learn_weights() -> dict[int, float]:
    return {int(k): v for k, v in _load_faker_learn()["weights"].items()}


def faker_daily_learn(date: str, picks: list[int], actual: list[int], prize_quality: float = 1.0) -> dict:
    """
    每日學習：根據實際開獎更新 Faker 號碼學習權重。
    核心目標：≥3顆命中（成本50元，三獎180元起才獲利）

    分層強化邏輯：
    - 命中5顆 → hit bonus ×4.0（最強記憶）
    - 命中4顆 → hit bonus ×3.0
    - 命中3顆 → hit bonus ×2.0（達獲利門檻，強化）
    - 命中0~2顆 → hit bonus ×1.0（正常，不過度懲罰）

    prize_quality: 獎金品質係數（1.0=正常，>1.0=中獎人少/高品質，<1.0=中獎人多/低品質）
    - 例：今日二獎<100注 → quality=1.3；二獎>300注 → quality=0.8
    - 讓系統更強記「分獎少」的情境，弱化「分獎多」情境
    """
    state = _load_faker_learn()
    hit_set = set(picks) & set(actual)
    hits    = len(hit_set)

    # 分層強化倍率（對應命中數）× 獎金品質係數
    TIER_MULTIPLIER = {0: 1.0, 1: 1.0, 2: 1.2, 3: 2.0, 4: 3.0, 5: 4.0}
    tier_mult = TIER_MULTIPLIER.get(hits, 1.0) * max(0.5, min(2.0, prize_quality))

    DECAY      = 0.90          # 整體溫和衰減（避免震盪太大）
    HIT_BONUS  = 2.2           # 基礎命中加成（× tier_mult）
    MISS_DECAY = 0.78          # 推薦但未中：懲罰

    # 衰減所有權重
    for k in state["weights"]:
        state["weights"][k] = float(state["weights"][k]) * DECAY

    # 命中號碼加分（tier 越高，加分越大）
    effective_bonus = HIT_BONUS * tier_mult
    for n in actual:
        key = str(n)
        if key in state["weights"]:
            state["weights"][key] = float(state["weights"][key]) * effective_bonus

    # 推薦但未命中：額外懲罰（命中越少懲罰越重，命中3+時減輕懲罰）
    miss_multiplier = 1.0 if hits >= 3 else (0.85 if hits == 2 else 0.78)
    missed_picks = set(picks) - set(actual)
    for n in missed_picks:
        key = str(n)
        if key in state["weights"]:
            state["weights"][key] = float(state["weights"][key]) * miss_multiplier

    # 正規化（防止數值爆炸）
    vals = list(state["weights"].values())
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: float(v) / avg for k, v in state["weights"].items()}

    # 記錄（含 hits_3plus 旗標）
    record = {
        "date": date, "picks": picks, "actual": actual,
        "hits": hits, "hits_3plus": 1 if hits >= 3 else 0,
    }
    state["history"].append(record)
    state["history"]      = state["history"][-180:]
    state["total_rounds"] += 1
    state["last_updated"]  = datetime.now(_TW).strftime("%Y-%m-%d %H:%M")

    recent30 = state["history"][-30:]
    state["avg_hits"]      = round(sum(r["hits"] for r in recent30) / len(recent30), 2) if recent30 else 0.0
    state["hits_3plus_rate"] = round(
        sum(r.get("hits_3plus", 0) for r in recent30) / len(recent30) * 100, 1
    ) if recent30 else 0.0

    _save_faker_learn(state)
    return {
        "date": date, "hits": hits,
        "avg_hits": state["avg_hits"],
        "hits_3plus_rate": state["hits_3plus_rate"],
        "total_rounds": state["total_rounds"],
    }


def faker_weekly_deep_learn() -> dict:
    """
    每週一深度學習：
    核心目標：≥3顆命中率（成本50元，三獎180元起才獲利）

    策略：
    1. 計算本週 vs 上週「≥3命中率」
    2. 高頻命中號碼加強（但排除生日/文化熱門號，維持獨得優勢）
    3. ≥3命中率 < 30% → 緊急加強（boost ×1.25）
    4. ≥3命中率 ≥ 50% → 保守學習（boost ×1.05，不過度調整）
    5. 保存週報（永久歷史）
    """
    state   = _load_faker_learn()
    history = state.get("history", [])

    this_week = history[-7:]     if len(history) >= 7  else history
    last_week = history[-14:-7]  if len(history) >= 14 else []

    def avg_h(lst):
        return round(sum(r["hits"] for r in lst) / len(lst), 2) if lst else 0.0

    def rate_3plus(lst):
        return round(sum(r.get("hits_3plus", 1 if r["hits"] >= 3 else 0) for r in lst)
                     / len(lst) * 100, 1) if lst else 0.0

    tw_avg    = avg_h(this_week)
    lw_avg    = avg_h(last_week)
    delta     = round(tw_avg - lw_avg, 2)
    tw_3plus  = rate_3plus(this_week)
    lw_3plus  = rate_3plus(last_week)
    delta_3p  = round(tw_3plus - lw_3plus, 1)

    # ── 依≥3命中率決定本週強化力度 ──────────────────────────────────
    if tw_3plus < 30:
        boost_actual = 1.25   # 緊急加強
        boost_hit    = 1.20
        mode_label   = "🚨 緊急模式（≥3命中率不足30%）"
    elif tw_3plus >= 50:
        boost_actual = 1.05   # 保守，勿過調
        boost_hit    = 1.04
        mode_label   = "✅ 穩定模式（≥3命中率≥50%）"
    else:
        boost_actual = 1.12   # 標準加強
        boost_hit    = 1.10
        mode_label   = "📈 標準強化模式"

    # 本週實際高頻號碼 → 加強，但跳過文化/生日熱門號（維持獨得策略）
    actual_counter = defaultdict(int)
    for r in this_week:
        for n in r.get("actual", []):
            actual_counter[n] += 1

    top_actual = sorted(NUM_RANGE, key=lambda n: -actual_counter.get(n, 0))[:12]
    # 過濾掉人氣最高的號碼（乘數 < 0.75 的），維持獨得優勢
    top_actual_filtered = [n for n in top_actual if _SOLO_WIN_MULTIPLIER.get(n, 1.0) >= 0.75][:10]

    for n in top_actual_filtered:
        key = str(n)
        state["weights"][key] = float(state["weights"].get(key, 1.0)) * boost_actual

    # 推薦且命中的號碼 → 再加一層（這些是「系統選對了」的號碼）
    hit_counter = defaultdict(int)
    for r in this_week:
        for n in set(r.get("picks", [])) & set(r.get("actual", [])):
            hit_counter[n] += 1
    top_hit = sorted(NUM_RANGE, key=lambda n: -hit_counter.get(n, 0))[:8]
    top_hit_filtered = [n for n in top_hit if _SOLO_WIN_MULTIPLIER.get(n, 1.0) >= 0.75]

    for n in top_hit_filtered:
        key = str(n)
        state["weights"][key] = float(state["weights"].get(key, 1.0)) * boost_hit

    # 正規化
    vals = list(state["weights"].values())
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: float(v) / avg for k, v in state["weights"].items()}

    # 更新 hits_3plus_rate
    recent30 = history[-30:]
    state["hits_3plus_rate"] = rate_3plus(recent30)
    _save_faker_learn(state)

    # 30期累計
    avg30     = round(sum(r["hits"] for r in history[-30:]) / len(history[-30:]), 2) if history else 0.0
    avg30_3p  = rate_3plus(history[-30:])
    gap_avg   = round(3.0 - tw_avg, 2)   # 目標改為3顆

    lines = [
        "🃏 Faker 策略 週度深度學習報告 (v5)",
        "═" * 36,
        f"{mode_label}",
        "",
        f"🗓️  本週（{len(this_week)}日）",
        f"   平均命中：{tw_avg:.2f} 顆（上週 {lw_avg:.2f}，{'+' if delta>=0 else ''}{delta:.2f}）",
        f"   ≥3顆命中率：{tw_3plus:.1f}%（上週 {lw_3plus:.1f}%，{'+' if delta_3p>=0 else ''}{delta_3p:.1f}%）",
        f"",
        f"🎯 獲利門檻（≥3顆）",
        f"   目標命中率 ≥ 40% | 本週 {tw_3plus:.1f}% | 差 {max(0, round(40 - tw_3plus, 1)):.1f}%",
        f"   平均命中目標 3.0 | 本週 {tw_avg:.2f} | 差 {max(0, gap_avg):.2f} 顆",
        f"",
        f"🔥 本週強化號碼（已過濾熱門號，維持獨得優勢）",
        f"   {top_actual_filtered}",
        f"",
        f"📊 近30期累計",
        f"   平均命中：{avg30:.2f} 顆",
        f"   ≥3顆命中率：{avg30_3p:.1f}%",
        f"   累計學習期數：{state['total_rounds']}",
        f"═" * 36,
    ]
    report_text = "\n".join(lines)

    # 永久保存週報
    weekly = _load_faker_weekly()
    weekly.append({
        "week_end":      datetime.now(_TW).strftime("%Y-%m-%d"),
        "tw_avg":        tw_avg,
        "lw_avg":        lw_avg,
        "delta":         delta,
        "tw_3plus":      tw_3plus,
        "lw_3plus":      lw_3plus,
        "avg30":         avg30,
        "avg30_3plus":   avg30_3p,
        "top_actual":    top_actual_filtered,
        "total_rounds":  state["total_rounds"],
        "report_text":   report_text,
    })
    _save_faker_weekly(weekly)

    return {
        "ok":            True,
        "tw_avg":        tw_avg,
        "lw_avg":        lw_avg,
        "delta":         delta,
        "tw_3plus":      tw_3plus,
        "lw_3plus":      lw_3plus,
        "avg30":         avg30,
        "avg30_3plus":   avg30_3p,
        "total_rounds":  state["total_rounds"],
        "report_text":   report_text,
    }


def get_faker_learn_summary() -> dict:
    state = _load_faker_learn()
    recent30 = state["history"][-30:]
    hits_3plus_rate = round(
        sum(r.get("hits_3plus", 1 if r["hits"] >= 3 else 0) for r in recent30)
        / len(recent30) * 100, 1
    ) if recent30 else 0.0
    return {
        "avg_hits":        state["avg_hits"],
        "hits_3plus_rate": hits_3plus_rate,   # ← 主要獲利指標
        "total_rounds":    state["total_rounds"],
        "last_updated":    state["last_updated"],
        "history":         state["history"][-7:],
        "recent_avg_hits": state["avg_hits"],  # 向後相容
    }


def faker_pick_from_records(records: list, window: int = 100) -> list[int]:
    """從指定 records 計算 Faker 推薦（避免用到當日資料）"""
    records = records[-window:]
    if not records:
        return []

    num_stats_raw = defaultdict(lambda: {
        "count": 0, "jackpot_total": 0, "second_total": 0,
        "sell_total": 0, "prize_total": 0,
    })
    for rec in records:
        for n in rec["numbers"]:
            s = num_stats_raw[n]
            s["count"]         += 1
            s["jackpot_total"] += rec["jackpot_count"]
            s["second_total"]  += rec["second_count"]
            s["sell_total"]    += rec["sell_amount"]
            s["prize_total"]   += rec["total_prize"]

    stats = {}
    for n in NUM_RANGE:
        s   = num_stats_raw[n]
        cnt = s["count"] or 1
        stats[n] = {
            "avg_second":      s["second_total"]  / cnt,
            "avg_jackpot":     s["jackpot_total"] / cnt,
            "avg_profit_rate": (s["sell_total"] - s["prize_total"]) / max(s["sell_total"], 1) * 100,
        }

    seconds      = [stats[n]["avg_second"]      for n in NUM_RANGE]
    jackpots     = [stats[n]["avg_jackpot"]      for n in NUM_RANGE]
    profit_rates = [stats[n]["avg_profit_rate"]  for n in NUM_RANGE]

    def norm_inv(values, i):
        mn, mx = min(values), max(values)
        return 1.0 - (values[i] - mn) / (mx - mn) if mx != mn else 0.5

    def norm(values, i):
        mn, mx = min(values), max(values)
        return (values[i] - mn) / (mx - mn) if mx != mn else 0.5

    scores = {}
    for i, n in enumerate(NUM_RANGE):
        base = (
            0.60 * norm_inv(seconds,      i) +
            0.25 * norm_inv(jackpots,     i) +
            0.15 * norm(profit_rates,     i)
        )
        scores[n] = base

    picked = sorted(NUM_RANGE, key=lambda n: -scores[n])[:5]
    return sorted(picked)


def faker_pick(window: int = 100) -> list[int]:
    """
    Faker 策略 v4（莊家思維版）

    核心邏輯：台彩要獲利最大 → 開出「少人買」的號碼 → 中獎人最少
    我們反推：找歷史上「開出時中獎人極少」的號碼 → 那就是台彩偏好開的號碼

    四層信號：
      35% 甜蜜點頻率   ── 歷史所有「頭獎≤2注 且 二獎<150注」期別中最常出現的號碼
      35% 500期低獎分  ── 近500期出現時平均中獎人最少的號碼（長期穩定信號）
      20% 近30期品質   ── 近30期中，「低中獎期」出現的號碼（近期趨勢）
      10% 迭代學習     ── 歷史命中修正（越中越強）
    """

    def _norm_inv(values: list, i: int) -> float:
        mn, mx = min(values), max(values)
        return 1.0 - (values[i] - mn) / (mx - mn) if mx != mn else 0.5

    def _norm(values: list, i: int) -> float:
        mn, mx = min(values), max(values)
        return (values[i] - mn) / (mx - mn) if mx != mn else 0.5

    data    = load_prize_data()
    records = data.get("records", [])

    # ── 信號A：甜蜜點頻率（自適應門檻，台彩獲利最高的期別）─────────
    # 計算每期「台彩獲利指數」= jackpot×10 + second（越低越好）
    # 取最低30%的期別（自適應，不受資料量影響）
    if records:
        profit_scores = sorted(records, key=lambda r: r["jackpot_count"] * 10 + r["second_count"])
        sweet_count   = max(int(len(profit_scores) * 0.30), 5)
        sweet         = profit_scores[:sweet_count]
    else:
        sweet = []
    sweet_freq: dict[int, int] = defaultdict(int)
    for r in sweet:
        for n in r["numbers"]:
            sweet_freq[n] += 1
    sweet_total = len(sweet) or 1
    sweet_vals  = [sweet_freq.get(n, 0) / sweet_total for n in NUM_RANGE]
    signal_a    = {n: _norm(sweet_vals, i) for i, n in enumerate(NUM_RANGE)}

    # ── 信號B：500期低中獎人數分析（長期穩定）──────────────────────
    stats500 = analyze_low_winner_numbers(window=500)
    if stats500:
        s2  = [stats500[n]["avg_second"]      for n in NUM_RANGE]
        j2  = [stats500[n]["avg_jackpot"]      for n in NUM_RANGE]
        pr2 = [stats500[n]["avg_profit_rate"]  for n in NUM_RANGE]
        signal_b = {
            n: (0.50 * _norm_inv(s2, i) +
                0.30 * _norm_inv(j2, i) +
                0.20 * _norm(pr2, i))
            for i, n in enumerate(NUM_RANGE)
        }
    else:
        signal_b = {n: 0.5 for n in NUM_RANGE}

    # ── 信號C：近30期品質加權（近期「低中獎期」的號碼優先）─────────
    recent30 = records[-30:] if len(records) >= 30 else records
    quality_freq: dict[int, float] = defaultdict(float)
    for r in recent30:
        # 品質分 = 1 / (1 + jackpot×10 + second)：中獎人越少品質越高
        q = 1.0 / (1.0 + r["jackpot_count"] * 10 + r["second_count"])
        for n in r["numbers"]:
            quality_freq[n] += q
    qf_vals  = [quality_freq.get(n, 0.0) for n in NUM_RANGE]
    signal_c = {n: _norm(qf_vals, i) for i, n in enumerate(NUM_RANGE)}

    # ── 信號D：迭代學習（命中修正，含獎金品質）─────────────────────
    learn_w  = get_faker_learn_weights()
    lw_vals  = list(learn_w.values())
    lw_mn, lw_mx = min(lw_vals), max(lw_vals)
    signal_d = {
        n: (learn_w.get(n, 1.0) - lw_mn) / (lw_mx - lw_mn) if lw_mx != lw_mn else 0.5
        for n in NUM_RANGE
    }

    # ── 信號E：群眾熱度迴避（實時爬蟲資料）────────────────────────
    # 使用 faker_crawler 的玩家購買熱度，高熱度 → 避開（分數低）
    signal_e = {n: 0.5 for n in NUM_RANGE}
    try:
        import faker_crawler as _fc
        crowd = _fc.load_crowd_data()
        if crowd and "popularity" in crowd:
            pop = crowd["popularity"]
            pop_vals = [float(pop.get(str(n), 0)) for n in NUM_RANGE]
            pop_mn, pop_mx = min(pop_vals), max(pop_vals)
            if pop_mx > pop_mn:
                # 反轉：人氣越高 → signal_e 越低（0.0）；人氣越低 → signal_e 越高（1.0）
                signal_e = {
                    n: 1.0 - (float(pop.get(str(n), pop_mn)) - pop_mn) / (pop_mx - pop_mn)
                    for n in NUM_RANGE
                }
    except Exception:
        pass

    # ── 整合評分（v5：加入群眾迴避信號）────────────────────────────
    scores = {
        n: (0.30 * signal_a.get(n, 0.5) +
            0.30 * signal_b.get(n, 0.5) +
            0.15 * signal_c.get(n, 0.5) +
            0.15 * signal_d.get(n, 0.5) +
            0.10 * signal_e.get(n, 0.5))
        for n in NUM_RANGE
    }

    picked = sorted(NUM_RANGE, key=lambda n: -scores[n])[:5]
    return sorted(picked)


def load_faker_tracker() -> dict:
    if FAKER_FILE.exists():
        try:
            return json.loads(FAKER_FILE.read_text())
        except Exception:
            pass
    return {"picks": [], "last_updated": None}


def save_faker_tracker(data: dict):
    FAKER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def update_faker_pick() -> dict:
    """
    每日 22:00 執行：
    1. 用「今日以前」的歷史資料算出今日推薦
    2. 直接對今日開獎做對獎（當日推薦 vs 當日開獎）
    3. 存檔並回傳結果
    """
    prize_data = load_prize_data()
    records = sorted(prize_data["records"], key=lambda r: r["period"])
    if not records:
        return {}

    latest  = records[-1]   # 今日最新開獎
    tracker = load_faker_tracker()
    picks   = tracker.get("picks", [])

    # 用不含今日的歷史資料算今日推薦
    history_records = records[:-1] if len(records) > 1 else records
    today_nums = faker_pick_from_records(history_records)

    # 對今日開獎直接結算
    actual = latest["numbers"]
    hits   = len(set(today_nums) & set(actual))

    picks.append({
        "date":    latest["date"],
        "period":  latest["period"],
        "numbers": today_nums,
        "actual":  actual,
        "hits":    hits,
    })
    picks = picks[-60:]

    avg_hits = round(sum(p["hits"] for p in picks if p["hits"] is not None) / len(picks), 2)

    tracker.update({
        "picks":        picks,
        "last_updated": datetime.now(_TW).strftime("%Y-%m-%d %H:%M"),
        "avg_hits":     avg_hits,
        "total_rounds": len(picks),
    })
    save_faker_tracker(tracker)

    # 計算獎金品質係數（中獎人數越少 → 品質越高）
    # 二獎人數基準：100注≈正常，<50注≈高品質，>300注≈低品質
    second_count = latest.get("second_count", 150)
    if second_count <= 50:
        prize_quality = 1.5    # 極少人中，強化學習
    elif second_count <= 100:
        prize_quality = 1.2
    elif second_count <= 200:
        prize_quality = 1.0    # 正常
    else:
        prize_quality = 0.8    # 很多人中，弱化學習（分獎情境）

    # 觸發每日學習（更新號碼學習權重，含獎金品質）
    faker_daily_learn(latest["date"], today_nums, actual, prize_quality=prize_quality)

    return {
        "date":      latest["date"],
        "numbers":   today_nums,
        "actual":    actual,
        "hits":      hits,
        "avg_hits":  avg_hits,
        "rounds":    len(picks),
    }


# ── 每日報告 ─────────────────────────────────────────────────────────────────

def daily_report_text() -> str:
    """產生每日 22:00 Telegram 報告文字"""
    data = load_prize_data()
    records = data["records"]
    if not records:
        return "❌ 尚無中獎人數資料"

    # 最近一期
    latest = sorted(records, key=lambda r: r["period"])[-1]
    num_stats = analyze_low_winner_numbers()

    date_str    = latest["date"]
    nums_str    = " ".join(f"{n:02d}" for n in latest["numbers"])
    profit_rate = round((latest["sell_amount"] - latest["total_prize"]) / max(latest["sell_amount"], 1) * 100, 1)

    # Faker 策略：當日推薦 vs 當日開獎
    faker        = update_faker_pick()
    faker_nums   = faker.get("numbers", [])
    faker_hits   = faker.get("hits", 0)
    faker_avg    = faker.get("avg_hits", 0.0)
    faker_rounds = faker.get("rounds", 0)

    hit_icons = "🎯" * faker_hits + "⬜" * (5 - faker_hits)

    learn_summary = get_faker_learn_summary()
    gap = round(TARGET_HITS - faker_avg, 2)

    lines = [
        f"🎯 今彩539 中獎分析報告",
        f"📅 {date_str}  期別：{latest['period']}",
        f"",
        f"🔢 今日開獎：{nums_str}",
        f"💰 銷售金額：{latest['sell_amount']:,}",
        f"🏆 頭獎：{latest['jackpot_count']} 注 × ${latest['jackpot_prize']:,}",
        f"🥈 二獎：{latest['second_count']} 注 × ${latest['second_prize']:,}",
        f"🥉 三獎：{latest['third_count']} 注 × ${latest['third_prize']:,}",
        f"4️⃣ 四獎：{latest['fourth_count']} 注 × ${latest['fourth_prize']:,}",
        f"📊 台彩獲利率：{profit_rate}%",
        f"",
        f"━━━━━ 🃏 Faker 策略 ━━━━━",
        f"",
        f"今日推薦：{'  '.join(f'{n:02d}' for n in faker_nums)}",
        f"今日開獎：{nums_str}",
        f"命中：{hit_icons}  {faker_hits}/5 顆",
        f"",
        f"📈 近30期平均命中：{faker_avg} 顆（目標5顆，差{gap:+.2f}）",
        f"   累計學習：{learn_summary.get('total_rounds', 0)} 期",
        f"   （539 隨機期望值：5×5/39 ≈ 0.64 顆）",
        f"",
        f"🕙 更新時間：{data.get('last_fetched', 'N/A')}",
    ]
    return "\n".join(lines)
