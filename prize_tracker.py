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


def update_prize_data() -> dict:
    """更新本月+上月資料，合併存檔"""
    now = datetime.now(_TW)
    this_month = now.strftime("%Y-%m")
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    existing = load_prize_data()
    existing_periods = {r["period"] for r in existing["records"]}

    new_records = []
    for month in [last_month, this_month]:
        for rec in fetch_prize_records(month, page_size=50):
            if rec["period"] not in existing_periods:
                new_records.append(rec)
                existing_periods.add(rec["period"])

    existing["records"].extend(new_records)
    # 保留最近 500 期
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


# ── 每日報告 ─────────────────────────────────────────────────────────────────

def daily_report_text() -> str:
    """產生每日 22:00 Telegram 報告文字"""
    data = load_prize_data()
    records = data["records"]
    if not records:
        return "❌ 尚無中獎人數資料"

    # 最近一期
    latest = sorted(records, key=lambda r: r["period"])[-1]
    analysis = find_low_winner_combinations()
    num_stats = analysis.get("num_analysis", {})

    date_str = latest["date"]
    nums_str = " ".join(f"{n:02d}" for n in latest["numbers"])
    profit_rate = round((latest["sell_amount"] - latest["total_prize"]) / max(latest["sell_amount"], 1) * 100, 1)

    # 今日號碼的「中獎人數評分」
    today_avg_second = sum(num_stats.get(n, {}).get("avg_second", 0) for n in latest["numbers"]) / 5

    low_winners = analysis["low_winner_numbers"]
    high_profit = analysis["high_profit_numbers"]
    sweet_top = [str(n) for n, _ in analysis["top_numbers"][:8]]

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
        f"── 策略分析（近 {len(records)} 期）──",
        f"🔵 出現時中獎人最少的號碼：",
        f"   {' '.join(f'{n:02d}' for n in low_winners)}",
        f"🟢 出現時台彩獲利最高的號碼：",
        f"   {' '.join(f'{n:02d}' for n in high_profit)}",
        f"⭐ 甜蜜點期別常見號碼（頭獎≤2注）：",
        f"   {' '.join(sweet_top)}",
        f"",
        f"📌 今日號碼平均二獎人數（歷史）：{today_avg_second:.0f} 注",
        f"   {'✅ 低於平均，符合低人氣策略' if today_avg_second < 170 else '⚠️ 高於平均，熱門號碼期'}",
        f"",
        f"🕙 更新時間：{data.get('last_fetched', 'N/A')}",
    ]
    return "\n".join(lines)
