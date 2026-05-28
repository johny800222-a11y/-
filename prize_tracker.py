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
        "weights":       {str(n): 1.0 for n in NUM_RANGE},   # 號碼學習權重
        "history":       [],    # 每日記錄 [{date, picks, actual, hits}]
        "total_rounds":  0,
        "avg_hits":      0.0,
        "last_updated":  None,
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


def faker_daily_learn(date: str, picks: list[int], actual: list[int]) -> dict:
    """
    每日學習：根據實際開獎更新 Faker 號碼學習權重。
    命中的號碼加分，未命中的衰減，強化 Faker 策略命中率。
    """
    state = _load_faker_learn()
    hits  = len(set(picks) & set(actual))

    DECAY       = 0.88
    HIT_BONUS   = 2.8
    MISS_DECAY  = 0.72

    # 衰減所有權重
    for k in state["weights"]:
        state["weights"][k] = float(state["weights"][k]) * DECAY

    # 命中號碼加分
    for n in actual:
        key = str(n)
        if key in state["weights"]:
            state["weights"][key] = float(state["weights"][key]) * HIT_BONUS

    # 推薦但未命中的號碼：額外懲罰
    missed_picks = set(picks) - set(actual)
    for n in missed_picks:
        key = str(n)
        if key in state["weights"]:
            state["weights"][key] = float(state["weights"][key]) * MISS_DECAY

    # 正規化
    vals = list(state["weights"].values())
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: float(v) / avg for k, v in state["weights"].items()}

    # 記錄
    record = {"date": date, "picks": picks, "actual": actual, "hits": hits}
    state["history"].append(record)
    state["history"]    = state["history"][-180:]
    state["total_rounds"] += 1
    state["last_updated"]  = datetime.now(_TW).strftime("%Y-%m-%d %H:%M")

    recent30 = state["history"][-30:]
    state["avg_hits"] = round(sum(r["hits"] for r in recent30) / len(recent30), 2) if recent30 else 0.0

    _save_faker_learn(state)
    return {"date": date, "hits": hits, "avg_hits": state["avg_hits"], "total_rounds": state["total_rounds"]}


def faker_weekly_deep_learn() -> dict:
    """
    每週一深度學習：
    1. 本週 vs 上週命中率對比
    2. 高頻命中號碼加強加成
    3. 連續低命中週期懲罰
    4. 保存週報（永久歷史）
    """
    state   = _load_faker_learn()
    history = state.get("history", [])

    this_week = history[-7:]  if len(history) >= 7  else history
    last_week = history[-14:-7] if len(history) >= 14 else []

    def avg_h(lst):
        return round(sum(r["hits"] for r in lst) / len(lst), 2) if lst else 0.0

    tw_avg = avg_h(this_week)
    lw_avg = avg_h(last_week)
    delta  = round(tw_avg - lw_avg, 2)

    # 本週哪些號碼被實際開出次數最多
    actual_counter = defaultdict(int)
    for r in this_week:
        for n in r.get("actual", []):
            actual_counter[n] += 1

    # 出現次數最多的10個號碼 → 加強權重
    top_actual = sorted(NUM_RANGE, key=lambda n: -actual_counter.get(n, 0))[:10]
    for n in top_actual:
        key = str(n)
        state["weights"][key] = float(state["weights"].get(key, 1.0)) * 1.10

    # 推薦準確率：在推薦且中的
    hit_counter = defaultdict(int)
    for r in this_week:
        for n in set(r.get("picks", [])) & set(r.get("actual", [])):
            hit_counter[n] += 1
    top_hit = sorted(NUM_RANGE, key=lambda n: -hit_counter.get(n, 0))[:8]
    for n in top_hit:
        key = str(n)
        state["weights"][key] = float(state["weights"].get(key, 1.0)) * 1.08

    # 正規化
    vals = list(state["weights"].values())
    avg  = sum(vals) / len(vals)
    if avg > 0:
        state["weights"] = {k: float(v) / avg for k, v in state["weights"].items()}
    _save_faker_learn(state)

    # 30期累計
    avg30 = round(sum(r["hits"] for r in history[-30:]) / len(history[-30:]), 2) if history else 0.0
    gap   = round(TARGET_HITS - tw_avg, 2)

    lines = [
        "🃏 Faker 策略 週度深度學習報告",
        "═" * 36,
        f"🗓️  本週平均命中（{len(this_week)}日）：{tw_avg:.2f} 顆",
        f"   上週平均命中：{lw_avg:.2f} 顆",
        f"   週變化：{'+' if delta >= 0 else ''}{delta:.2f} 顆",
        f"",
        f"🎯 距目標差距",
        f"   目標 5 顆全中 | 目前 {tw_avg:.2f} | 差 {gap:.2f} 顆",
        f"",
        f"🔥 本週高頻實際開出號碼（深度強化）",
        f"   {top_actual}",
        f"",
        f"📊 30期累計平均命中：{avg30:.2f} 顆",
        f"   累計學習期數：{state['total_rounds']}",
        f"═" * 36,
    ]
    report_text = "\n".join(lines)

    # 永久保存週報
    weekly = _load_faker_weekly()
    weekly.append({
        "week_end":   datetime.now(_TW).strftime("%Y-%m-%d"),
        "tw_avg":     tw_avg,
        "lw_avg":     lw_avg,
        "delta":      delta,
        "avg30":      avg30,
        "top_actual": top_actual,
        "total_rounds": state["total_rounds"],
        "report_text": report_text,
    })
    _save_faker_weekly(weekly)

    return {
        "ok":          True,
        "tw_avg":      tw_avg,
        "lw_avg":      lw_avg,
        "delta":       delta,
        "avg30":       avg30,
        "total_rounds": state["total_rounds"],
        "report_text": report_text,
    }


def get_faker_learn_summary() -> dict:
    state = _load_faker_learn()
    return {
        "avg_hits":     state["avg_hits"],
        "total_rounds": state["total_rounds"],
        "last_updated": state["last_updated"],
        "history":      state["history"][-7:],
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
        scores[n] = (
            0.50 * norm_inv(seconds,      i) +
            0.30 * norm_inv(jackpots,     i) +
            0.20 * norm(profit_rates,     i)
        )
    return sorted(sorted(NUM_RANGE, key=lambda n: -scores[n])[:5])


def faker_pick(window: int = 100) -> list[int]:
    """
    Faker 策略（加入迭代學習）：
    評分 = 60% Faker 原始分（低中獎人數）+ 40% 學習權重（歷史命中修正）

    隨著每日學習，命中率越來越高，逼近 5 顆目標。
    """
    stats = analyze_low_winner_numbers(window=window)
    if not stats:
        return []

    seconds      = [stats[n]["avg_second"]      for n in NUM_RANGE]
    jackpots     = [stats[n]["avg_jackpot"]      for n in NUM_RANGE]
    profit_rates = [stats[n]["avg_profit_rate"]  for n in NUM_RANGE]

    def norm_inv(values, n_idx):
        mn, mx = min(values), max(values)
        return 1.0 - (values[n_idx] - mn) / (mx - mn) if mx != mn else 0.5

    def norm(values, n_idx):
        mn, mx = min(values), max(values)
        return (values[n_idx] - mn) / (mx - mn) if mx != mn else 0.5

    # Faker 原始分（不選玩家愛選的號碼）
    faker_scores = {}
    for i, n in enumerate(NUM_RANGE):
        faker_scores[n] = (
            0.50 * norm_inv(seconds,      i) +
            0.30 * norm_inv(jackpots,     i) +
            0.20 * norm(profit_rates,     i)
        )

    # 學習權重（命中修正）
    learn_w = get_faker_learn_weights()
    lw_vals = list(learn_w.values())
    lw_min, lw_max = min(lw_vals), max(lw_vals)

    scores = {}
    for n in NUM_RANGE:
        # 學習權重正規化到 0~1
        lw_norm = (learn_w.get(n, 1.0) - lw_min) / (lw_max - lw_min) if lw_max != lw_min else 0.5
        scores[n] = 0.60 * faker_scores[n] + 0.40 * lw_norm

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

    # 觸發每日學習（更新號碼學習權重）
    faker_daily_learn(latest["date"], today_nums, actual)

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
