"""
Bingo Bingo 投注追蹤 + 每日損益報告
每日12/16/20點各買6星×10期、9星×10期，基準資金1000元
"""

import json
import pytz
from pathlib import Path
from datetime import datetime, timedelta

import bingo_core

_TW = pytz.timezone("Asia/Taipei")

_DATA_DIR    = Path("/data") if Path("/data").exists() else Path(__file__).parent
TRACKER_FILE = _DATA_DIR / "bingo_invest.json"

# ── 賠率設定（每注） ─────────────────────────────────────────────
COST_6 = 50    # 每注50元
COST_9 = 50    # 每注50元（9星同價）
PERIODS_PER_SLOT = 10   # 每個時段買幾期
STARTING_BALANCE = 1000

# 6星：選6個號碼，依命中數給獎
PRIZE_6 = {6: 10000, 5: 300, 4: 50, 3: 0, 2: 0, 1: 0, 0: 0}
# 9星：選9個號碼，依命中數給獎
PRIZE_9 = {9: 80000, 8: 4000, 7: 400, 6: 50, 5: 0, 4: 0, 3: 0, 2: 0, 1: 0, 0: 0}

NUM_COLS = bingo_core.NUM_COLS


# ── 資料讀寫 ──────────────────────────────────────────────────────

def _load() -> dict:
    if TRACKER_FILE.exists():
        try:
            return json.loads(TRACKER_FILE.read_text())
        except Exception:
            pass
    return {
        "balance": STARTING_BALANCE,
        "total_bet": 0,
        "total_win": 0,
        "snapshots": [],   # 待結算的快照
        "daily_logs": [],  # 已結算的每日記錄
    }


def _save(data: dict):
    TRACKER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── 快照儲存（12/16/20點呼叫） ────────────────────────────────────

def save_snapshot(slot_label: str, six: list[int], nine: list[int], latest_period: str):
    """
    記錄當前推薦，並標記投注從 latest_period 之後的10期。
    slot_label: "12:00" / "16:00" / "20:00"
    """
    data = _load()
    # 去重：同一天同一時段同一期已存在則略過
    today = datetime.now(_TW).strftime("%Y-%m-%d")
    for s in data["snapshots"]:
        if s.get("date") == today and s.get("slot") == slot_label and s.get("from_period") == latest_period:
            return
    now = datetime.now(_TW).strftime("%Y-%m-%d %H:%M")
    snap = {
        "date":    datetime.now(_TW).strftime("%Y-%m-%d"),
        "slot":    slot_label,
        "saved_at": now,
        "six":     six,
        "nine":    nine,
        "from_period": latest_period,
        "settled": False,
        "results": [],
    }
    data["snapshots"].append(snap)
    _save(data)


# ── 結算快照 ─────────────────────────────────────────────────────

def _count_hits(nums_drawn: list[int], pick: list[int]) -> int:
    return sum(1 for n in pick if n in set(nums_drawn))


def settle_snapshots(df):
    """對未結算的快照，嘗試與實際開獎配對，計算損益。"""
    data = _load()
    changed = False

    for snap in data["snapshots"]:
        if snap["settled"]:
            continue

        # 以 period 數值排序，找 from_period 之後的 10 期
        from_p = int(float(snap["from_period"]))
        df_sorted = df.copy()
        df_sorted["_period_int"] = df_sorted["period"].astype(float).astype(int)
        df_sorted = df_sorted.sort_values("_period_int").reset_index(drop=True)

        after_df = df_sorted[df_sorted["_period_int"] > from_p]
        if len(after_df) < PERIODS_PER_SLOT:
            continue  # 不足10期，等下次

        target_rows = after_df.head(PERIODS_PER_SLOT)

        slot_bet    = PERIODS_PER_SLOT * (COST_6 + COST_9)
        slot_win    = 0
        results     = []

        for _, row in target_rows.iterrows():
            drawn = [int(row[c]) for c in NUM_COLS]

            h6 = _count_hits(drawn, snap["six"])
            h9 = _count_hits(drawn, snap["nine"])
            w6 = PRIZE_6.get(h6, 0)
            w9 = PRIZE_9.get(h9, 0)
            slot_win += w6 + w9

            results.append({
                "period": str(row["period"]),
                "six_hits": h6, "six_win": w6,
                "nine_hits": h9, "nine_win": w9,
            })

        snap["settled"]  = True
        snap["results"]  = results
        snap["bet"]      = slot_bet
        snap["win"]      = slot_win
        snap["net"]      = slot_win - slot_bet

        data["balance"]   += slot_win - slot_bet
        data["total_bet"] += slot_bet
        data["total_win"] += slot_win
        changed = True

    if changed:
        _save(data)
    return data


# ── 每日報告 ────────────────────────────────────────────────────

def daily_report_text(df) -> str:
    data = settle_snapshots(df)
    today    = datetime.now(_TW).strftime("%Y-%m-%d")
    yesterday = (datetime.now(_TW) - timedelta(days=1)).strftime("%Y-%m-%d")

    # 昨日已結算快照
    yest_snaps = [s for s in data["snapshots"]
                  if s.get("date") == yesterday and s.get("settled")]

    y_bet = sum(s["bet"] for s in yest_snaps)
    y_win = sum(s["win"] for s in yest_snaps)
    y_net = y_win - y_bet

    # 命中統計
    all_results = [r for s in yest_snaps for r in s.get("results", [])]
    total_6bets = len(all_results)
    hit6_any    = sum(1 for r in all_results if r["six_hits"] >= 4)
    hit9_any    = sum(1 for r in all_results if r["nine_hits"] >= 6)
    win_rate_6  = round(hit6_any / total_6bets * 100) if total_6bets else 0
    win_rate_9  = round(hit9_any / total_6bets * 100) if total_6bets else 0

    # 累計
    balance  = data["balance"]
    cum_net  = data["total_win"] - data["total_bet"]
    cum_roi  = round(cum_net / data["total_bet"] * 100) if data["total_bet"] else 0

    lines = [
        f"📊 Bingo Bingo 每日報告 — {yesterday}",
        "─" * 36,
        f"昨日投注：{y_bet:,} 元",
        f"昨日獲獎：{y_win:,} 元",
        f"昨日損益：{'▲' if y_net >= 0 else '▼'} {y_net:+,} 元",
        "",
        f"6星命中率（≥4球）：{win_rate_6}%（{hit6_any}/{total_6bets} 期）",
        f"9星命中率（≥6球）：{win_rate_9}%（{hit9_any}/{total_6bets} 期）",
        "",
        "─" * 36,
        f"累計投注：{data['total_bet']:,} 元",
        f"累計獲獎：{data['total_win']:,} 元",
        f"累計損益：{'▲' if cum_net >= 0 else '▼'} {cum_net:+,} 元（{cum_roi:+}%）",
        f"目前餘額：{balance:,} 元",
        "─" * 36,
        "⚠️ 本報告為統計模擬，不構成投資建議",
    ]

    # 加入昨日各時段明細
    if yest_snaps:
        lines.insert(7, "")
        lines.insert(8, "昨日時段明細：")
        for s in yest_snaps:
            best = max(s["results"], key=lambda r: r["six_win"] + r["nine_win"], default=None)
            best_str = f"最佳期 {best['period']}（6星{best['six_hits']}中/{best['nine_hits']}中）" if best else ""
            lines.insert(9, f"  {s['slot']}  投{s['bet']} 贏{s['win']} 淨{s['net']:+}  {best_str}")

    return "\n".join(lines)


def daily_report_html(df) -> str:
    text = daily_report_text(df)
    html = "<pre style='font-family:monospace;font-size:14px;line-height:1.6'>"
    html += text.replace("▲", "<span style='color:#2ecc71'>▲</span>") \
                .replace("▼", "<span style='color:#e74c3c'>▼</span>")
    html += "</pre>"
    return html


# ── 寄信 ────────────────────────────────────────────────────────

def send_daily_email(html_body: str):
    import os, smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    to_email  = os.environ.get("REPORT_EMAIL", "johny800222@gmail.com")

    if not smtp_user or not smtp_pass:
        return  # 未設定則略過

    today = datetime.now(_TW).strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 Bingo Bingo 每日報告 {today}"
    msg["From"]    = smtp_user
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())
