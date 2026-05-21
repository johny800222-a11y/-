"""
今彩539 Web App — Flask 後端
"""

import json
import threading
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import core
import learner
import bingo_core
import bingo_tracker
import bingo_learner
import prize_tracker
import backup_manager

app = Flask(__name__)

# 全量抓取進度
_fetch_state = {"running": False, "page": 0, "total": 256, "error": ""}

# 推薦快取（主推薦 + 策略推薦），同一期不重複產生
_rec_file      = core.DATA_FILE.parent / "current_rec.json"
_strategy_file = core.DATA_FILE.parent / "current_strategy_rec.json"


def _load_rec() -> dict:
    if _rec_file.exists():
        try:
            return json.loads(_rec_file.read_text())
        except Exception:
            pass
    return {}


def _save_rec(draw_date: str, nums: list):
    _rec_file.write_text(json.dumps({"draw_date": draw_date, "best": nums}))


def _load_strategy_rec() -> dict:
    if _strategy_file.exists():
        try:
            return json.loads(_strategy_file.read_text())
        except Exception:
            pass
    return {}


def _save_strategy_rec(draw_date: str, data: dict):
    _strategy_file.write_text(json.dumps({"draw_date": draw_date, **data}))


def _get_or_generate_rec(df) -> list[int]:
    """主推薦：同一期回傳快取，新一期先學習再產生"""
    latest_date = df["date"].max().strftime("%Y-%m-%d")
    cached = _load_rec()

    if cached.get("draw_date") == latest_date:
        return cached["best"]

    # 新一期：學習後產生推薦
    old_best     = cached.get("best", [])
    old_strategy = _load_strategy_rec().get("nums", [])
    if old_best:
        learner.auto_update_from_df(df, old_best, old_best, last_strategy=old_strategy)

    weights = learner.get_weights()
    best = core.recommend_best(df, weights)
    _save_rec(latest_date, best)
    return best


def _get_or_generate_strategy(df) -> dict:
    """策略推薦：同一期回傳快取，新一期重新產生"""
    latest_date = df["date"].max().strftime("%Y-%m-%d")
    cached = _load_strategy_rec()

    if cached.get("draw_date") == latest_date:
        return cached

    result = core.strategy_recommend(df)
    _save_strategy_rec(latest_date, result)
    return {"draw_date": latest_date, **result}


# ── 頁面 ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    df = core.load_data()
    return jsonify({
        "ready":    df is not None,
        "fetching": _fetch_state["running"],
        "page":     _fetch_state["page"],
        "total":    _fetch_state["total"],
        "error":    _fetch_state["error"],
    })


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    if _fetch_state["running"]:
        return jsonify({"ok": False, "msg": "已在下載中"})

    def _run():
        _fetch_state.update(running=True, page=0, error="")
        try:
            core.fetch_all(progress_cb=lambda p, t: _fetch_state.update(page=p))
        except Exception as e:
            _fetch_state["error"] = str(e)
        finally:
            _fetch_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/update", methods=["POST"])
def api_update():
    df = core.load_data()
    if df is None:
        return jsonify({"ok": False, "msg": "請先下載資料"})
    try:
        df, updated = core.update_latest()
        return jsonify({"ok": True, "updated": updated, "total": len(df)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/stats")
def api_stats():
    df = core.load_data()
    if df is None:
        return jsonify({"ok": False, "msg": "尚無資料"})
    stats = core.get_stats(df)
    learn = learner.get_summary()
    return jsonify({"ok": True, **stats, "learn": learn})


@app.route("/api/recommend")
def api_recommend():
    df = core.load_data()
    if df is None:
        return jsonify({"ok": False, "msg": "尚無資料"})

    best = _get_or_generate_rec(df)
    cached = _load_rec()

    strategy = _get_or_generate_strategy(df)

    return jsonify({
        "ok":        True,
        "best":      best,
        "draw_date": cached.get("draw_date", ""),
        "strategy":  strategy,
    })


@app.route("/api/learn/history")
def api_learn_history():
    return jsonify(learner.get_summary())


# ── Bingo ─────────────────────────────────────────────────────────────────────

@app.route("/bingo")
def bingo_index():
    return render_template("bingo.html")


_bingo_winrate_cache = {"data": None, "at": 0}


def _get_winrate(df):
    import time as _time
    now = _time.time()
    if _bingo_winrate_cache["data"] and now - _bingo_winrate_cache["at"] < 300:
        return _bingo_winrate_cache["data"]
    wr = bingo_core.winrate_24h(df)
    _bingo_winrate_cache.update(data=wr, at=now)
    return wr


@app.route("/api/bingo/stats")
def api_bingo_stats():
    df = bingo_core.load_data()
    if df is None or df.empty:
        try:
            df = bingo_core.init_data()
        except Exception as e:
            return jsonify({"ok": False, "msg": f"資料初始化失敗：{e}"})
    if df is None or df.empty:
        return jsonify({"ok": False, "msg": "尚無資料"})

    probs     = bingo_core.model_probs(df)
    np_list   = bingo_core.num_probs(df, probs)
    t10       = bingo_core.top10(np_list)
    bs        = bingo_core.guess_bigsmall(np_list)
    oe        = bingo_core.guess_oddeven(np_list)
    try:
        wr = _get_winrate(df)
    except Exception as e:
        wr = {"error": str(e)}
    latest    = df.iloc[-1]
    latest_time = str(latest.get("time", "")) if "time" in latest else ""

    return jsonify({
        "ok":          True,
        "total_draws": len(df),
        "latest_time": latest_time,
        "probs":       probs,
        "num_probs":   np_list,
        "top10":       t10,
        "bigsmall":    bs,
        "oddeven":     oe,
        "winrate":     wr,
    })


@app.route("/api/bingo/update", methods=["POST"])
def api_bingo_update():
    try:
        df, updated = bingo_core.update_latest()
        total = len(df) if df is not None else 0
        return jsonify({"ok": True, "updated": updated, "total": total})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/bingo/pick")
def api_bingo_pick():
    df = bingo_core.load_data()
    if df is None or df.empty:
        return jsonify({"ok": False, "msg": "尚無資料"})
    try:
        result = bingo_core.smart_pick(df)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/bingo/ping")
def api_bingo_ping():
    """測試 Railway 是否能連到 pilio.idv.tw"""
    try:
        import requests as req
        r = req.get("https://www.pilio.idv.tw/bingo/list.asp",
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        return jsonify({"ok": True, "status": r.status_code, "len": len(r.content)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/bingo/report")
def bingo_report():
    df = bingo_core.load_data()
    if df is None or df.empty:
        return "尚無資料", 503
    text = bingo_tracker.daily_report_text(df)
    html = bingo_tracker.daily_report_html(df)
    return f"""<!DOCTYPE html><html><head><meta charset=UTF-8>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Bingo 損益報告</title>
    <style>body{{background:#0a0a1a;color:#e0e0f0;padding:20px;font-family:monospace}}</style>
    </head><body>{html}</body></html>"""


@app.route("/api/bingo/snapshot/<slot>", methods=["POST"])
def api_bingo_snapshot(slot):
    """手動觸發投注快照，slot = 1200 / 1600 / 2000"""
    labels = {"1200": "12:00", "1600": "16:00", "1700": "17:00", "2000": "20:00"}
    label = labels.get(slot)
    if not label:
        return jsonify({"ok": False, "msg": "slot 需為 1200/1600/1700/2000"})
    _take_snapshot(label)
    data = bingo_tracker._load()
    last = data["snapshots"][-1] if data["snapshots"] else {}
    return jsonify({"ok": True, "slot": label,
                    "six": last.get("six"), "nine": last.get("nine"),
                    "from_period": last.get("from_period")})


@app.route("/api/bingo/patch_1700", methods=["POST"])
def api_bingo_patch_1700():
    """一次性補回 2026-05-06 17:00 快照結算記錄"""
    data = bingo_tracker._load()
    snap_1700 = {
        "date": "2026-05-06", "slot": "17:00",
        "saved_at": "2026-05-06 17:00",
        "six":  [15, 30, 34, 56, 65, 74],
        "nine": [15, 30, 34, 41, 56, 60, 64, 65, 74],
        "from_period": "115025492",
        "settled": True,
        "bet": 1000, "win": 100, "net": -900,
        "results": [
            {"period":"115025397","six_hits":2,"six_win":0,"nine_hits":3,"nine_win":0},
            {"period":"115025396","six_hits":2,"six_win":0,"nine_hits":2,"nine_win":0},
            {"period":"115025395","six_hits":2,"six_win":0,"nine_hits":4,"nine_win":0},
            {"period":"115025394","six_hits":3,"six_win":0,"nine_hits":4,"nine_win":0},
            {"period":"115025393","six_hits":0,"six_win":0,"nine_hits":1,"nine_win":0},
            {"period":"115025392","six_hits":3,"six_win":0,"nine_hits":5,"nine_win":0},
            {"period":"115025391","six_hits":2,"six_win":0,"nine_hits":2,"nine_win":0},
            {"period":"115025390","six_hits":2,"six_win":0,"nine_hits":2,"nine_win":0},
            {"period":"115025389","six_hits":4,"six_win":50,"nine_hits":6,"nine_win":50},
            {"period":"115025388","six_hits":1,"six_win":0,"nine_hits":2,"nine_win":0},
        ]
    }
    # 插到最前面（按時間順序）
    data["snapshots"].insert(0, snap_1700)
    data["balance"]   += 100 - 1000   # win 100, bet 1000 → net -900
    data["total_bet"] += 1000
    data["total_win"] += 100
    bingo_tracker._save(data)
    return jsonify({"ok": True, "balance": data["balance"],
                    "total_bet": data["total_bet"], "total_win": data["total_win"]})


@app.route("/api/data/export")
def api_data_export():
    """匯出 /data 目錄下所有資料檔案（供本機開發同步用）"""
    import base64
    from flask import send_file
    files = {}
    data_dir = bingo_core._DATA_DIR
    for fname in ["bingo_history.csv", "bingo_invest.json", "bingo_learn.json",
                  "539_history.csv", "learn_state.json", "current_rec.json"]:
        fpath = data_dir / fname
        if fpath.exists():
            content = fpath.read_bytes()
            files[fname] = base64.b64encode(content).decode()
    return jsonify({"ok": True, "files": files})


@app.route("/api/bingo/resettle", methods=["POST"])
def api_bingo_resettle():
    """把所有快照標為未結算，重新用正確期數順序結算"""
    data = bingo_tracker._load()
    for s in data["snapshots"]:
        s["settled"] = False
        s["results"] = []
        s.pop("bet", None); s.pop("win", None); s.pop("net", None)
    data["balance"]   = bingo_tracker.STARTING_BALANCE
    data["total_bet"] = 0
    data["total_win"] = 0
    bingo_tracker._save(data)
    # 立刻重新結算
    df = bingo_core.load_data()
    if df is not None:
        data = bingo_tracker.settle_snapshots(df)
    return jsonify({"ok": True, "balance": data["balance"],
                    "total_bet": data["total_bet"], "total_win": data["total_win"],
                    "settled": sum(1 for s in data["snapshots"] if s["settled"])})


@app.route("/api/bingo/reset", methods=["POST"])
def api_bingo_reset():
    """保留最新一筆快照，其餘全部清除，餘額重置為1000"""
    data = bingo_tracker._load()
    if data["snapshots"]:
        latest = max(data["snapshots"], key=lambda s: s.get("saved_at", ""))
        latest["settled"] = False
        latest["results"] = []
        latest.pop("bet", None)
        latest.pop("win", None)
        latest.pop("net", None)
        data["snapshots"] = [latest]
    else:
        data["snapshots"] = []
    data["balance"]   = bingo_tracker.STARTING_BALANCE
    data["total_bet"] = 0
    data["total_win"] = 0
    data["daily_logs"] = []
    bingo_tracker._save(data)
    return jsonify({"ok": True, "kept": data["snapshots"][0].get("slot") if data["snapshots"] else None})


@app.route("/api/bingo/learn")
def api_bingo_learn():
    """查看學習狀態"""
    summary = bingo_learner.get_summary()
    return jsonify({"ok": True, "morning_log": _morning_log, **summary})


@app.route("/api/bingo/send_report", methods=["POST"])
def api_bingo_send_report():
    """手動或外部 Cron 觸發：結算 + 學習 + 發送 TG 報告"""
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            return jsonify({"ok": False, "msg": "無開獎資料"})
        data = bingo_tracker.settle_snapshots(df)
        result = bingo_learner.daily_update(data, df)
        learn_summary = bingo_learner.get_summary()
        text = bingo_tracker.daily_report_text(df, learn_summary)
        _send_telegram(text)
        return jsonify({"ok": True, "learn": result, "tg_error": _morning_log.get("error")})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "msg": str(e), "trace": traceback.format_exc()})


@app.route("/api/backup", methods=["POST"])
def api_backup():
    """手動觸發備份"""
    result = backup_manager.send_backup_to_telegram()
    return jsonify(result)


@app.route("/api/backup/status")
def api_backup_status():
    return jsonify(backup_manager.get_status())


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    """從最新備份還原（或指定 file_id）"""
    file_id = request.json.get("file_id") if request.is_json else None
    result = backup_manager.restore_from_telegram(file_id)
    return jsonify(result)


@app.route("/api/prize/update", methods=["POST"])
def api_prize_update():
    """手動更新 539 中獎人數資料"""
    try:
        result = prize_tracker.update_prize_data()
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/prize/report")
def api_prize_report():
    """查看中獎人數分析"""
    try:
        analysis = prize_tracker.find_low_winner_combinations()
        data = prize_tracker.load_prize_data()
        latest = sorted(data["records"], key=lambda r: r["period"])[-1] if data["records"] else {}
        return jsonify({"ok": True, "latest": latest, "analysis": analysis,
                        "total_records": len(data["records"]),
                        "last_fetched": data.get("last_fetched")})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/prize/send_report", methods=["POST"])
def api_prize_send_report():
    """手動觸發 TG 報告"""
    try:
        prize_tracker.update_prize_data()
        text = prize_tracker.daily_report_text()
        _send_telegram(text)
        return jsonify({"ok": True, "tg_error": _morning_log.get("error")})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/bingo/report")
def api_bingo_report():
    df = bingo_core.load_data()
    if df is None or df.empty:
        return jsonify({"ok": False, "msg": "尚無資料"})
    data = bingo_tracker.settle_snapshots(df)
    return jsonify({"ok": True, **data})


@app.route("/api/bingo/pages")
def api_bingo_pages():
    """逐頁測試抓取結果"""
    import requests as req
    results = []
    for page in range(1, 6):
        try:
            url = f"https://www.pilio.idv.tw/bingo/list.asp?indexpage={page}"
            r = req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.encoding = "big5"
            recs = bingo_core._parse_page(r.text)
            results.append({"page": page, "status": r.status_code, "count": len(recs),
                            "first": recs[0]["period"] if recs else None,
                            "last":  recs[-1]["period"] if recs else None})
        except Exception as e:
            results.append({"page": page, "error": str(e)})
    return jsonify(results)


@app.route("/api/bingo/debug")
def api_bingo_debug():
    """除錯：直接顯示從網站抓到幾筆、最新 period"""
    try:
        import requests as req
        r = req.get("https://www.pilio.idv.tw/bingo/list.asp",
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.encoding = "big5"
        recs = bingo_core._parse_page(r.text)
        df = bingo_core.load_data()
        existing_periods = set(df["period"].astype(str).tolist()) if df is not None else set()
        new_periods = [rec["period"] for rec in recs if rec["period"] not in existing_periods]
        return jsonify({
            "ok": True,
            "fetched": len(recs),
            "new_periods": len(new_periods),
            "sample_periods": [rec["period"] for rec in recs[:3]],
            "existing_total": len(existing_periods),
            "http_status": r.status_code,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


def _auto_update():
    df = core.load_data()
    if df is None:
        return
    try:
        df, updated = core.update_latest()
        # 不論是否有新資料，都嘗試觸發學習（學習模組內部會判斷是否已學）
        _get_or_generate_rec(df)
    except Exception:
        pass


def _bingo_auto_update():
    try:
        df = bingo_core.load_data()
        if df is None or df.empty or len(df) < 400:
            bingo_core.init_data()
        else:
            df, _ = bingo_core.update_latest()
        # 順帶結算未完成的快照
        df2 = bingo_core.load_data()
        if df2 is not None and not df2.empty:
            bingo_tracker.settle_snapshots(df2)
    except Exception:
        pass


def _take_snapshot(slot_label: str):
    """在指定時段擷取推薦並記錄投注快照（使用時段專屬學習權重）"""
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            return
        # 取時段專屬學習權重，讓推薦更精準
        slot_weights = bingo_learner.get_weights_for_slot(slot_label)
        pick = bingo_core.smart_pick(df, learn_weights=slot_weights)
        latest_period = str(int(df["period"].astype(float).max()))
        bingo_tracker.save_snapshot(slot_label, pick["six"], pick["nine"], latest_period)
    except Exception:
        pass


_morning_log = {"last_run": None, "result": None, "error": None}


def _send_telegram(text: str):
    """傳送 Telegram 訊息"""
    import os, requests as _req, json as _json
    token   = os.environ.get("TG_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        _morning_log["error"] = "TG_TOKEN 或 TG_CHAT_ID 未設定"
        return
    try:
        r = _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15
        )
        result = r.json()
        if not result.get("ok"):
            _morning_log["error"] = f"TG error: {result.get('description','')}"
        else:
            _morning_log["error"] = None
    except Exception as e:
        _morning_log["error"] = f"TG exception: {e}"


def _bingo_midnight_learn():
    """每日 00:05：Bingo 全日開獎結束，結算快照 + 迭代更新學習權重 + 備份"""
    import traceback
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            return
        data = bingo_tracker.settle_snapshots(df)
        result = bingo_learner.daily_update(data, df)
        _morning_log.update(last_run=_tw_now(), result=result, error=None)
        # 迭代完成後立刻備份
        backup_manager.send_backup_to_telegram()
    except Exception:
        import traceback as tb
        _morning_log.update(last_run=_tw_now(), result=None, error=tb.format_exc())


def _morning_routine():
    """早上9點：傳送每日報告到 Telegram（學習已於 00:00 完成）"""
    import traceback
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            _morning_log.update(last_run=_tw_now(), result=None, error="無開獎資料")
            return
        # 確保快照已結算（補保險）
        data = bingo_tracker.settle_snapshots(df)
        # 取學習摘要（00:00 已更新過）
        learn_summary = bingo_learner.get_summary()
        text = bingo_tracker.daily_report_text(df, learn_summary)
        _send_telegram(text)
        html = bingo_tracker.daily_report_html(df)
        bingo_tracker.send_daily_email(html)
    except Exception:
        _morning_log.update(last_run=_tw_now(), result=None, error=traceback.format_exc())



def _faker_noon_report():
    """每日 12:00 傳送 Faker 今日推薦到 TG"""
    try:
        nums = prize_tracker.faker_pick()
        nums_str = "  ".join(f"{n:02d}" for n in nums)
        from datetime import datetime
        now = datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
        text = f"🃏 Faker 今日推薦\n📅 {now}\n\n{nums_str}\n\n（台彩獲利最高組合）"
        _send_telegram(text)
    except Exception:
        pass


def _prize_report_routine():
    """週一~週六 22:00：抓取最新中獎人數資料並傳送分析報告到 TG"""
    try:
        prize_tracker.update_prize_data()
        text = prize_tracker.daily_report_text()
        _send_telegram(text)
    except Exception:
        import traceback
        _morning_log["error"] = traceback.format_exc()


def _tw_now() -> str:
    return datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")


def _539_weekly_learn():
    """週一~週六 21:05 對 539 做迭代學習 + 備份"""
    try:
        df = core.load_data()
        if df is None or df.empty:
            return
        cached = _load_rec()
        old_best = cached.get("best", [])
        if old_best:
            learner.auto_update_from_df(df, old_best, old_best)
        weights = learner.get_weights()
        best = core.recommend_best(df, weights)
        latest_date = df["date"].max().strftime("%Y-%m-%d")
        _save_rec(latest_date, best)
        # 迭代完成後立刻備份
        backup_manager.send_backup_to_telegram()
    except Exception:
        pass


scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Taipei"))
scheduler.add_job(_auto_update,           "cron", day_of_week="mon-sat", hour=21, minute=0)
scheduler.add_job(_539_weekly_learn,      "cron", day_of_week="mon-sat", hour=21, minute=5)
scheduler.add_job(_bingo_auto_update,     "interval", minutes=5)
# 每日投注快照（12:00 / 16:00 / 20:00）
scheduler.add_job(_take_snapshot, "cron", hour=12, minute=0,  args=["12:00"])
scheduler.add_job(_take_snapshot, "cron", hour=16, minute=0,  args=["16:00"])
scheduler.add_job(_take_snapshot, "cron", hour=20, minute=0,  args=["20:00"])
# 00:00 Bingo 迭代學習（開獎 07:05~23:55 結束後）
scheduler.add_job(_bingo_midnight_learn,  "cron", hour=0, minute=5)
# 09:00 傳送每日 TG 報告
scheduler.add_job(_morning_routine,       "cron", hour=9, minute=0)
# 每日 12:00 傳送 Faker 推薦
scheduler.add_job(_faker_noon_report,     "cron", day_of_week="mon-sat", hour=12, minute=0)
# 22:00 傳送 539 中獎人數分析報告
scheduler.add_job(_prize_report_routine,  "cron", day_of_week="mon-sat", hour=22, minute=0)
scheduler.start()

# 啟動時若 Bingo 無資料則自動初始化（背景執行，不阻塞啟動）
threading.Thread(target=_bingo_auto_update, daemon=True).start()

# 啟動時若 539 無資料則自動全量抓取
def _539_auto_init():
    if core.load_data() is None:
        try:
            core.fetch_all()
        except Exception:
            pass

threading.Thread(target=_539_auto_init, daemon=True).start()


# 啟動補發：若今天 09:00 後尚未發送報告，則在啟動後 60 秒補發
def _startup_catchup():
    import time as _time
    _time.sleep(60)  # 等資料載入完成
    try:
        tz = pytz.timezone("Asia/Taipei")
        now = datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")
        # 補發 Bingo 每日報告
        sent_today = _morning_log.get("last_run", "")
        if now.hour >= 9 and not sent_today.startswith(today_str):
            _morning_routine()
        # 補學 539（確保每次啟動都對齊最新開獎）
        df539 = core.load_data()
        if df539 is not None and not df539.empty:
            _get_or_generate_rec(df539)
        # 補學 Bingo（若 00:05 已過且尚未學習）
        if now.hour >= 0 and not sent_today.startswith(today_str):
            _bingo_midnight_learn()
    except Exception:
        pass

from datetime import datetime
threading.Thread(target=_startup_catchup, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=5539)
