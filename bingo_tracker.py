"""
Bingo Bingo 投注追蹤 + 每日損益報告
每日12/16/20點各買6星×10期（$250）、9星×10期（$250），基準資金1000元
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
COST_6 = 25    # 每注25元（6星，10期共250元）
COST_9 = 25    # 每注25元（9星，10期共250元）
PERIODS_PER_SLOT = 10   # 每個時段買幾期
STARTING_BALANCE = 1000

# 6星：選6個號碼，依命中數給獎（台彩官方賠率）
# 中6→$25,000 / 中5→$1,000 / 中4→$200 / 中3→$25 / 其餘→$0
PRIZE_6 = {6: 25000, 5: 1000, 4: 200, 3: 25, 2: 0, 1: 0, 0: 0}

# 9星：選9個號碼，依命中數給獎（台彩官方賠率）
# 中9→$1,000,000 / 中8→$100,000 / 中7→$3,000 / 中6→$500
# 中5→$100 / 中4→$25 / 中0→$25 / 其餘→$0
PRIZE_9 = {9: 1_000_000, 8: 100_000, 7: 3_000, 6: 500, 5: 100, 4: 25, 3: 0, 2: 0, 1: 0, 0: 25}

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

def save_snapshot(slot_label: str, six: list[int], nine: list[int], latest_period: str,
                  strategy_nominations: dict = None):
    """
    記錄當前推薦，並標記投注從 latest_period 之後的10期。
    slot_label: "12:00" / "16:00" / "20:00"
    strategy_nominations: 各策略的提名（供學習器追蹤命中率）
    """
    data = _load()
    today = datetime.now(_TW).strftime("%Y-%m-%d")
    for s in data["snapshots"]:
        if s.get("date") == today and s.get("slot") == slot_label and s.get("from_period") == latest_period:
            return
    now = datetime.now(_TW).strftime("%Y-%m-%d %H:%M")
    snap = {
        "date":    today,
        "slot":    slot_label,
        "saved_at": now,
        "six":     six,
        "nine":    nine,
        "from_period": latest_period,
        "strategy_nominations": strategy_nominations or {},
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

        after_df = df_sorted[df_sorted["_period_int"] > from_p].drop_duplicates("_period_int")
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

            # 記錄實際開獎號碼（供學習器精準學習：知道哪幾顆命中）
            results.append({
                "period":    str(row["period"]),
                "drawn":     sorted(drawn),   # ← 實際開獎號碼
                "six_hits":  h6, "six_win":  w6,
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

def daily_report_text(df, learn_summary: dict = None) -> str:
    data = settle_snapshots(df)
    yesterday = (datetime.now(_TW) - timedelta(days=1)).strftime("%Y-%m-%d")

    # 昨日已結算快照
    yest_snaps = [s for s in data["snapshots"]
                  if s.get("date") == yesterday and s.get("settled")]

    # 命中統計
    all_results = [r for s in yest_snaps for r in s.get("results", [])]
    total_bets  = len(all_results)
    hit6_any    = sum(1 for r in all_results if r["six_hits"] >= 4)
    hit9_any    = sum(1 for r in all_results if r["nine_hits"] >= 6)
    win_rate_6  = round(hit6_any / total_bets * 100) if total_bets else 0
    win_rate_9  = round(hit9_any / total_bets * 100) if total_bets else 0

    # 累計
    balance = data["balance"]
    t_bet   = data["total_bet"]
    t_win   = data["total_win"]
    cum_net = t_win - t_bet
    cum_roi = round(cum_net / t_bet * 100) if t_bet else 0

    lines = [
        f"📊 Bingo Bingo 每日報告 — {yesterday}",
        "─" * 34,
    ]

    # 昨日各時段明細
    if yest_snaps:
        lines.append("📌 昨日時段明細")
        lines.append("")
        slot_num = 1
        for s in yest_snaps:
            results = s.get("results", [])
            s_results = [r for r in results]
            best = max(s_results, key=lambda r: r["six_hits"] + r["nine_hits"], default=None)
            best_str = f"最佳：6星{best['six_hits']}中 / 9星{best['nine_hits']}中" if best else ""
            lines.append(f"{slot_num}️⃣ {s['slot']}")
            lines.append(f"   推薦 6星：{s.get('six', [])}")
            lines.append(f"   推薦 9星：{s.get('nine', [])}")
            lines.append(f"   投注 {s.get('bet',0):,} 元 / 獲獎 {s.get('win',0):,} 元 / 淨 {s.get('net',0):+,} 元")
            lines.append(f"   {best_str}")
            lines.append("")
            slot_num += 1
        lines.append(f"6星命中率（≥4球）：{win_rate_6}%（{hit6_any}/{total_bets} 期）")
        lines.append(f"9星命中率（≥6球）：{win_rate_9}%（{hit9_any}/{total_bets} 期）")
    else:
        lines.append("昨日無已結算記錄")

    # 命中率統計
    lines += [
        f"🎯 命中率",
        f"   6星（≥4球中獎）：{win_rate_6}%（{hit6_any}/{total_bets} 期）",
        f"   9星（≥6球中獎）：{win_rate_9}%（{hit9_any}/{total_bets} 期）",
        "",
    ]

    # 演算法迭代資訊
    if learn_summary:
        lines += [
            "🔄 演算法迭代",
            f"   累計學習：{learn_summary.get('total_rounds', 0)} 期",
            f"   近期 6星平均命中：{learn_summary.get('avg_six_hits', 0)} 球",
            f"   近期 9星平均命中：{learn_summary.get('avg_nine_hits', 0)} 球",
            f"   最後更新：{learn_summary.get('last_updated', '-')}",
            "",
        ]

    lines += [
        "─" * 34,
        "📊 累計損益",
        f"   累計投注：{t_bet:,} 元",
        f"   累計獲獎：{t_win:,} 元",
        f"   累計損益：{'▲' if cum_net >= 0 else '▼'} {cum_net:+,} 元（{cum_roi:+}%）",
        f"   目前餘額：{balance:,} 元",
        "─" * 34,
        "⚠️ 本報告為統計模擬，不構成投資建議",
    ]

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


def recalc_all_settled() -> dict:
    """用正確賠率重新計算所有已結算快照的獎金，修正 balance/total_bet/total_win。
    歷史原始開獎資料（hits）不動，只重算 win 金額。"""
    data = _load()
    new_total_bet = 0
    new_total_win = 0

    for snap in data["snapshots"]:
        if not snap.get("settled"):
            continue
        slot_bet = PERIODS_PER_SLOT * (COST_6 + COST_9)
        slot_win = 0
        for r in snap.get("results", []):
            w6 = PRIZE_6.get(r["six_hits"], 0)
            w9 = PRIZE_9.get(r["nine_hits"], 0)
            r["six_win"]  = w6
            r["nine_win"] = w9
            slot_win += w6 + w9
        snap["bet"] = slot_bet
        snap["win"] = slot_win
        snap["net"] = slot_win - slot_bet
        new_total_bet += slot_bet
        new_total_win += slot_win

    data["total_bet"] = new_total_bet
    data["total_win"] = new_total_win
    data["balance"]   = STARTING_BALANCE + new_total_win - new_total_bet
    _save(data)
    return {
        "ok": True,
        "snapshots_recalc": sum(1 for s in data["snapshots"] if s.get("settled")),
        "total_bet": new_total_bet,
        "total_win": new_total_win,
        "balance":   data["balance"],
    }
