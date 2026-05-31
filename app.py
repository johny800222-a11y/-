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
import virtual_bingo
import castle_war

app = Flask(__name__)

# 全量抓取進度
_fetch_state = {"running": False, "page": 0, "total": 256, "error": ""}

# 推薦快取（三策略獨立快取）
_rec_file      = core.DATA_FILE.parent / "current_rec.json"
_value_file    = core.DATA_FILE.parent / "current_value_rec.json"
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


def _load_value_rec() -> dict:
    if _value_file.exists():
        try:
            return json.loads(_value_file.read_text())
        except Exception:
            pass
    return {}


def _save_value_rec(draw_date: str, nums: list):
    _value_file.write_text(json.dumps({"draw_date": draw_date, "nums": nums}))


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
    """策略A（機率推薦）：同一期回傳快取，新一期先對三策略學習再產生"""
    latest_date = df["date"].max().strftime("%Y-%m-%d")
    cached_a = _load_rec()

    if cached_a.get("draw_date") == latest_date:
        return cached_a["best"]

    # 新一期：用上一期的三組推薦來學習
    old_prob     = cached_a.get("best", [])
    old_value    = _load_value_rec().get("nums", [])
    old_strategy = _load_strategy_rec().get("nums", [])

    if old_prob:
        learner.auto_update_from_df(df, old_prob, old_value or old_prob, last_strategy=old_strategy)

    weights = learner.get_weights()
    best = core.recommend_best(df, weights)
    _save_rec(latest_date, best)
    return best


def _get_or_generate_value(df) -> list[int]:
    """策略B（價值推薦）：近期短波 + 號碼對相關"""
    latest_date = df["date"].max().strftime("%Y-%m-%d")
    cached = _load_value_rec()

    if cached.get("draw_date") == latest_date:
        return cached["nums"]

    # 確保 A 先執行（學習已完成）
    _get_or_generate_rec(df)

    weights = learner.get_weights()
    nums = core.value_recommend(df, weights)
    _save_value_rec(latest_date, nums)
    return nums


def _get_or_generate_strategy(df) -> dict:
    """策略C（結構策略推薦）：同一期回傳快取，新一期重新產生"""
    latest_date = df["date"].max().strftime("%Y-%m-%d")
    cached = _load_strategy_rec()

    if cached.get("draw_date") == latest_date:
        return cached

    result = core.strategy_recommend(df)
    _save_strategy_rec(latest_date, result)
    return {"draw_date": latest_date, **result}


def _get_ensemble(df) -> list[int]:
    """整合推薦：根據三策略近期勝率動態加權"""
    weights = learner.get_weights()
    scores  = learner.get_ensemble_scores()
    return core.ensemble_recommend(df, weights, scores)


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

    best     = _get_or_generate_rec(df)        # 策略A：機率推薦
    value    = _get_or_generate_value(df)      # 策略B：價值推薦
    strategy = _get_or_generate_strategy(df)   # 策略C：結構策略
    ensemble = _get_ensemble(df)               # 整合推薦（三策略加權投票）
    cached   = _load_rec()
    scores   = learner.get_ensemble_scores()

    return jsonify({
        "ok":        True,
        "best":      best,       # 策略A
        "value":     value,      # 策略B
        "ensemble":  ensemble,   # 整合推薦
        "draw_date": cached.get("draw_date", ""),
        "strategy":  strategy,   # 策略C（含詳細分析）
        "ensemble_scores": scores,
    })


@app.route("/api/learn/history")
def api_learn_history():
    return jsonify(learner.get_summary())


@app.route("/api/learn/weekly_report")
def api_weekly_report():
    """手動觸發週度學習報告"""
    return jsonify(learner.weekly_deep_analysis())


@app.route("/api/learn/weekly_history")
def api_learn_weekly_history():
    """查看 539 所有週報歷史（永久保存）"""
    return jsonify({"ok": True, "history": learner.get_weekly_history()})


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


@app.route("/api/bingo/weekly_report")
def api_bingo_weekly_report():
    """手動觸發 Bingo 週度深度學習報告"""
    try:
        df     = bingo_core.load_data()
        report = bingo_learner.weekly_deep_learn(df)
        return jsonify(report)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/bingo/weekly_history")
def api_bingo_weekly_history():
    """查看所有週報歷史（永久保存）"""
    return jsonify({"ok": True, "history": bingo_learner.get_weekly_history()})


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


# ── Virtual Bingo ─────────────────────────────────────────────────────────────

@app.route("/virtual")
def virtual_index():
    return render_template("virtual_bingo.html")

@app.route("/virtual/castle")
def virtual_castle():
    return render_template("castle_war.html")


@app.route("/api/virtual/users")
def api_virtual_users():
    users = virtual_bingo.list_users()
    return jsonify({"users": users})


@app.route("/api/virtual/login", methods=["POST"])
def api_virtual_login():
    data = request.get_json() or {}
    user = virtual_bingo.verify_login(data.get("user_id",""), data.get("password",""))
    if user:
        stats = virtual_bingo.get_user_stats(user["id"])
        return jsonify({"ok": True, "user": {**user, **stats, "user_id": user["id"]}})
    return jsonify({"ok": False, "msg": "帳號或密碼錯誤"})


@app.route("/api/virtual/stats")
def api_virtual_stats():
    uid = request.args.get("user_id","")
    stats = virtual_bingo.get_user_stats(uid)
    if not stats:
        return jsonify({"ok": False, "msg": "帳號不存在"})
    return jsonify({"ok": True, "stats": stats})


@app.route("/api/virtual/bet", methods=["POST"])
def api_virtual_bet():
    data = request.get_json() or {}
    uid          = data.get("user_id","")
    repeat_draws = int(data.get("repeat_draws", 1))
    result = virtual_bingo.place_bet(
        user_id        = uid,
        bet_type       = data.get("bet_type","直玩"),
        pick_count     = int(data.get("pick_count", 5)),
        balls          = data.get("balls", []),
        dan_balls      = data.get("dan_balls", []),
        tuo_balls      = data.get("tuo_balls", []),
        multiplier     = int(data.get("multiplier", 1)),
        repeat_draws   = repeat_draws,
        current_period = str(data.get("current_period", "")),
    )
    # 下注成功 → 發放城池點數
    if result.get("ok"):
        user = virtual_bingo.get_user(uid)
        name = user["name"] if user else uid
        castle_war.earn_points(uid, name, repeat_draws)
        result["castle_points_earned"] = castle_war.calc_bet_points(repeat_draws)
    return jsonify(result)


@app.route("/api/virtual/bets")
def api_virtual_bets():
    uid    = request.args.get("user_id","")
    status = request.args.get("status","all")
    bets   = virtual_bingo.get_user_bets(uid, limit=60,
                status=None if status=="all" else status)
    return jsonify({"ok": True, "bets": bets})


@app.route("/api/virtual/debug_bets_file")
def api_debug_bets_file():
    """臨時診斷：確認 virtual_bets.json 實際狀況"""
    import os
    path = virtual_bingo.BETS_FILE
    exists = path.exists()
    size = path.stat().st_size if exists else 0
    raw = ""
    parse_err = ""
    count = 0
    if exists:
        try:
            raw = path.read_text()[:500]
            data = json.loads(path.read_text())
            count = len(data.get("bets", []))
        except Exception as e:
            parse_err = str(e)
    return jsonify({"exists": exists, "size": size, "count": count,
                    "parse_err": parse_err, "preview": raw})


@app.route("/api/virtual/leaderboard")
def api_virtual_leaderboard():
    return jsonify({"ok": True, "board": virtual_bingo.get_leaderboard()})


@app.route("/api/virtual/latest_draw")
def api_virtual_latest_draw():
    """取最新一期 Bingo 開獎號碼（供投注介面顯示）"""
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            return jsonify({"ok": False})
        # 以 period 數值最大者為最新（避免 time=NaN 的排序問題）
        latest = df.loc[df["period"].astype(float).idxmax()]
        period = str(int(float(latest["period"])))
        # 欄位名稱為 n1~n20
        ball_cols = sorted([c for c in df.columns if c.startswith("n") and c[1:].isdigit()],
                           key=lambda x: int(x[1:]))
        balls = [int(latest[c]) for c in ball_cols if latest[c] > 0]
        draw_date = str(latest.get("date", "")) if "date" in latest.index else ""
        draw_time = str(latest.get("time", "")) if "time" in latest.index else ""
        # NaN 轉空字串
        if draw_date in ("nan", "NaT"): draw_date = ""
        if draw_time in ("nan", "NaT"): draw_time = ""
        return jsonify({
            "ok": True, "period": period, "balls": balls[:20],
            "draw_date": draw_date, "draw_time": draw_time
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/virtual/init", methods=["POST"])
def api_virtual_init():
    """初始化帳號（僅首次執行）"""
    data = request.get_json() or {}
    accounts = data.get("accounts", [])
    if not accounts:
        return jsonify({"ok": False, "msg": "請提供 accounts 清單"})
    result = virtual_bingo.init_users(accounts)
    return jsonify(result)


@app.route("/api/virtual/reset_balance", methods=["POST"])
def api_virtual_reset_balance():
    """管理員重置餘額"""
    data = request.get_json() or {}
    result = virtual_bingo.reset_user_balance(
        data.get("user_id",""), int(data.get("amount", 10000))
    )
    return jsonify(result)


@app.route("/api/virtual/settle", methods=["POST"])
def api_virtual_settle():
    """手動觸發結算（最新一期）"""
    try:
        df = bingo_core.load_data()
        result = virtual_bingo.auto_settle_from_df(df)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/virtual/reset_bet", methods=["POST"])
def api_virtual_reset_bet():
    """清除錯誤結算並重新補算"""
    try:
        data   = request.get_json() or {}
        bet_id = data.get("bet_id", "")
        df     = bingo_core.load_data()
        result = virtual_bingo.reset_and_rerun_bet(bet_id, df)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/virtual/backfill_settle", methods=["POST"])
def api_virtual_backfill_settle():
    """
    補跑歷史期次結算
    body: { "date": "2026/5/23", "time_from": "14:00", "time_to": "15:00" }
    """
    try:
        data      = request.get_json() or {}
        df        = bingo_core.load_data()
        result    = virtual_bingo.backfill_settle(
            df,
            date_str  = data.get("date"),
            time_from = data.get("time_from"),
            time_to   = data.get("time_to"),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/virtual/change_name", methods=["POST"])
def api_virtual_change_name():
    data = request.get_json() or {}
    result = virtual_bingo.change_name(data.get("user_id", ""), data.get("name", ""))
    return jsonify(result)


@app.route("/api/virtual/change_password", methods=["POST"])
def api_virtual_change_pw():
    data = request.get_json() or {}
    result = virtual_bingo.change_password(
        data.get("user_id",""), data.get("old_password",""), data.get("new_password","")
    )
    return jsonify(result)


@app.route("/api/virtual/weekly_champions")
def api_virtual_weekly_champions():
    """取最近 20 週冠軍記錄"""
    return jsonify(virtual_bingo.get_weekly_champions(20))


@app.route("/api/virtual/save_weekly_champion", methods=["POST"])
def api_virtual_save_weekly_champion():
    """手動觸發週冠軍快照（管理員用）"""
    result = virtual_bingo.save_weekly_champion()
    return jsonify(result)


@app.route("/api/virtual/reset_all_balances", methods=["POST"])
def api_virtual_reset_all_balances():
    """手動重置所有玩家本金（管理員用）"""
    result = virtual_bingo.weekly_reset_all_balances()
    castle_war.weekly_reset()   # 同步重置城池
    return jsonify(result)


# ── 城池大戰 API ──────────────────────────────────────────────────────────────
@app.route("/api/castle/state")
def api_castle_state():
    return jsonify({"ok": True, "castles": castle_war.get_all_castles(),
                    "log": castle_war.get_battle_log(20)})

@app.route("/api/castle/recruit", methods=["POST"])
def api_castle_recruit():
    d = request.get_json() or {}
    return jsonify(castle_war.recruit(d.get("user_id",""), d.get("unit",""), int(d.get("count",1))))

@app.route("/api/castle/upgrade_wall", methods=["POST"])
def api_castle_upgrade_wall():
    d = request.get_json() or {}
    return jsonify(castle_war.upgrade_wall(d.get("user_id","")))

@app.route("/api/castle/buy_shield", methods=["POST"])
def api_castle_buy_shield():
    d = request.get_json() or {}
    return jsonify(castle_war.buy_shield(d.get("user_id","")))

@app.route("/api/castle/upgrade_hp", methods=["POST"])
def api_castle_upgrade_hp():
    d = request.get_json() or {}
    return jsonify(castle_war.upgrade_hp(d.get("user_id","")))

@app.route("/api/castle/revive", methods=["POST"])
def api_castle_revive():
    d = request.get_json() or {}
    return jsonify(castle_war.revive(d.get("user_id","")))

@app.route("/api/castle/attack", methods=["POST"])
def api_castle_attack():
    d = request.get_json() or {}
    army = {k: int(d.get(k, 0)) for k in ["soldier","elite","catapult"]}
    return jsonify(castle_war.attack(d.get("attacker_id",""), d.get("defender_id",""), army))

@app.route("/api/castle/init", methods=["POST"])
def api_castle_init():
    d = request.get_json() or {}
    result = castle_war.ensure_castle(d.get("user_id",""), d.get("name",""))
    return jsonify({"ok": True, "castle": result})

@app.route("/api/castle/admin_reset", methods=["POST"])
def api_castle_admin_reset():
    d = request.get_json() or {}
    if d.get("secret") != "syuan_admin_2026":
        return jsonify({"ok": False, "msg": "unauthorized"}), 403
    hp = int(d.get("hp", 500))
    bonus = int(d.get("bonus_pts", 0))
    result = castle_war.admin_reset_all(hp=hp, bonus_pts=bonus)
    return jsonify(result)

@app.route("/api/castle/admin_patch", methods=["POST"])
def api_castle_admin_patch():
    d = request.get_json() or {}
    if d.get("secret") != "syuan_admin_2026":
        return jsonify({"ok": False, "msg": "unauthorized"}), 403
    result = castle_war.admin_patch_castle(
        user_id=d.get("user_id",""),
        hp=d.get("hp"),
        points_add=int(d.get("points_add", 0)),
        unban=bool(d.get("unban", False))
    )
    return jsonify(result)


# ── Chat ──────────────────────────────────────────────────────────────────────

@app.route("/api/virtual/chat")
def api_virtual_chat_get():
    limit    = int(request.args.get("limit", 50))
    since_id = request.args.get("since_id", None)
    msgs = virtual_bingo.get_chat(limit=limit, since_id=since_id)
    return jsonify({"messages": msgs})


@app.route("/api/virtual/chat", methods=["POST"])
def api_virtual_chat_send():
    data = request.get_json() or {}
    result = virtual_bingo.send_chat(
        data.get("user_id", ""),
        data.get("text", ""),
    )
    return jsonify(result)


@app.route("/api/virtual/chat/reaction", methods=["POST"])
def api_virtual_chat_reaction():
    data = request.get_json() or {}
    result = virtual_bingo.add_reaction(
        data.get("user_id", ""),
        data.get("msg_id", ""),
        data.get("emoji", ""),
    )
    return jsonify(result)


@app.route("/api/virtual/chat/delete", methods=["POST"])
def api_virtual_chat_delete():
    data = request.get_json() or {}
    result = virtual_bingo.delete_chat_msg(
        data.get("user_id", ""),
        data.get("msg_id", ""),
    )
    return jsonify(result)


# ── Prize ─────────────────────────────────────────────────────────────────────

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


@app.route("/api/faker/learn")
def api_faker_learn():
    """Faker 學習狀態"""
    return jsonify({"ok": True, **prize_tracker.get_faker_learn_summary()})


@app.route("/api/faker/weekly_report")
def api_faker_weekly_report():
    """手動觸發 Faker 週度深度學習報告"""
    try:
        report = prize_tracker.faker_weekly_deep_learn()
        return jsonify(report)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/faker/weekly_history")
def api_faker_weekly_history():
    """Faker 週報歷史（永久保存）"""
    from pathlib import Path
    import json
    f = Path("/data/faker_learn_weekly.json") if Path("/data").exists() else Path(__file__).parent / "faker_learn_weekly.json"
    data = json.loads(f.read_text()) if f.exists() else []
    return jsonify({"ok": True, "history": data})


@app.route("/api/faker/crowd_analysis")
def api_faker_crowd_analysis():
    """Faker 爬蟲分析結果（玩家熱度 + 台彩獲利最高組合）"""
    try:
        import faker_crawler
        data = faker_crawler.get_faker_recommendation()
        report = faker_crawler.generate_report_text(data)
        return jsonify({"ok": True, "data": data, "report": report})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/faker/refresh_crowd", methods=["POST"])
def api_faker_refresh_crowd():
    """手動觸發 Faker 爬蟲更新（重新抓取玩家熱度 + 推薦分析）"""
    try:
        import faker_crawler
        data = faker_crawler.update_crowd_data()
        report = faker_crawler.generate_report_text(data)
        _send_telegram(report)
        return jsonify({"ok": True, "top5": data.get("top5", [])})
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


@app.route("/api/bingo/recalc", methods=["POST"])
def api_bingo_recalc():
    """管理員：用正確賠率重算所有已結算快照的獎金"""
    d = request.get_json() or {}
    if d.get("secret") != "syuan_admin_2026":
        return jsonify({"ok": False, "msg": "unauthorized"}), 403
    result = bingo_tracker.recalc_all_settled()
    return jsonify(result)

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
            # 同步結算虛擬投注
            try:
                virtual_bingo.auto_settle_from_df(df2)
            except Exception:
                pass
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
        bingo_tracker.save_snapshot(
            slot_label, pick["six"], pick["nine"], latest_period,
            strategy_nominations=pick.get("strategy_nominations", {})
        )
    except Exception:
        pass


_morning_log  = {"last_run": None, "result": None, "error": None}
_morning_sent = {"date": ""}   # 防重複鎖：同一天只推播一次


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
    """每日 00:05：Bingo 全日開獎結束，結算快照 + 迭代更新學習權重 + 備份（不傳TG，09:00統一傳）"""
    import traceback as tb
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            return
        data   = bingo_tracker.settle_snapshots(df)
        result = bingo_learner.daily_update(data, df)
        _morning_log.update(last_run=_tw_now(), result=result, error=None)
        backup_manager.send_backup_to_telegram()
    except Exception:
        _morning_log.update(last_run=_tw_now(), result=None, error=tb.format_exc())


def _bingo_weekly_deep_learn():
    """每週一 00:10：Bingo 深度學習（靜默），週報併入 _weekly_evolution_report"""
    try:
        df = bingo_core.load_data()
        if df is not None:
            bingo_tracker.settle_snapshots(df)
            bingo_learner.weekly_deep_learn(df)   # 學習 + 保存，不單獨推播
    except Exception:
        pass


def _morning_routine():
    """早上9點：傳送 Bingo 每日報告 + 539 今日推薦 到 Telegram（學習已於 00:00 完成）"""
    import traceback
    today = datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
    if _morning_sent["date"] == today:
        return   # 今天已發過，防止重啟補發重複推播
    _morning_sent["date"] = today
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            _morning_log.update(last_run=_tw_now(), result=None, error="無開獎資料")
            return
        data          = bingo_tracker.settle_snapshots(df)
        learn_summary = bingo_learner.get_summary()

        # Bingo 損益報告 + 命中統計（含迭代進度）
        result = _morning_log.get("result") or {}
        avg6   = result.get("avg_six_hits",  learn_summary.get("avg_six_hits",  0))
        avg9   = result.get("avg_nine_hits", learn_summary.get("avg_nine_hits", 0))
        gap6   = round(3.0 - avg6, 2)
        gap9   = round(4.0 - avg9, 2)
        bingo_text = bingo_tracker.daily_report_text(df, learn_summary)
        learn_line = (
            f"\n📈 迭代進度\n"
            f"   6星平均：{avg6:.2f} 顆（目標3.0，差{gap6:+.2f}）\n"
            f"   9星平均：{avg9:.2f} 顆（目標4.0，差{gap9:+.2f}）\n"
            f"   累計學習：{learn_summary.get('total_rounds', 0)} 期"
        )
        _send_telegram(bingo_text + learn_line)
        html = bingo_tracker.daily_report_html(df)
        bingo_tracker.send_daily_email(html)
    except Exception:
        _morning_log.update(last_run=_tw_now(), result=None, error=traceback.format_exc())



_faker_noon_sent = {"date": ""}   # 防重複鎖

def _faker_noon_report():
    """每日 12:00 傳送 Faker 今日推薦到 TG（防重複：同一天只送一次）"""
    try:
        import datetime as _dt
        today = _dt.datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
        if _faker_noon_sent["date"] == today:
            return   # 今天已送過，略過
        _faker_noon_sent["date"] = today

        nums = prize_tracker.faker_pick()
        nums_str = "  ".join(f"{n:02d}" for n in nums)
        text = f"🃏 Faker 今日推薦\n📅 {today}\n\n{nums_str}\n\n（台彩獲利最高組合）"
        _send_telegram(text)
    except Exception:
        pass


def _prize_report_routine():
    """週一~週六 22:00：抓取最新中獎人數 + Faker 爬蟲更新 + 每日迭代 + 傳送 TG"""
    try:
        prize_tracker.update_prize_data()
        # Faker 爬蟲：更新玩家熱度 + 網路推薦 + 重算台彩獲利最高組合
        try:
            import faker_crawler
            faker_crawler.update_crowd_data()
        except Exception:
            pass
        text = prize_tracker.daily_report_text()   # 內部已觸發 faker_daily_learn
        _send_telegram(text)
    except Exception:
        import traceback
        _morning_log["error"] = traceback.format_exc()


def _faker_weekly_deep_learn():
    """每週一 09:10：Faker 深度學習 + 週報推播 TG"""
    try:
        report = prize_tracker.faker_weekly_deep_learn()
        if report.get("ok"):
            _send_telegram(report["report_text"])
    except Exception:
        pass


def _tw_now() -> str:
    return datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")


def _539_daily_learn():
    """週一~週六 21:05 每日迭代：三策略分別學習 + 重新生成推薦"""
    try:
        df = core.load_data()
        if df is None or df.empty:
            return

        # 讀取昨日三策略推薦
        old_prob     = _load_rec().get("best", [])
        old_value    = _load_value_rec().get("nums", [])
        old_strategy = _load_strategy_rec().get("nums", [])

        if old_prob:
            result = learner.auto_update_from_df(
                df,
                last_prob=old_prob,
                last_value=old_value or old_prob,
                last_strategy=old_strategy,
            )
            if result:
                msg = (
                    f"🎯 539 每日迭代完成 {result['date']}\n"
                    f"實際開獎：{result['actual']}\n"
                    f"A機率命中：{result['prob_hits']}/5\n"
                    f"B價值命中：{result['value_hits']}/5\n"
                    f"C策略命中：{result['strategy_hits']}/5\n"
                    f"整合權重 A:{result['ensemble_scores'].get('prob',1):.2f} "
                    f"B:{result['ensemble_scores'].get('value',1):.2f} "
                    f"C:{result['ensemble_scores'].get('strategy',1):.2f}"
                )
                _send_telegram(msg)

        # 重新生成三策略推薦（無效化快取讓下次呼叫重算）
        latest_date = df["date"].max().strftime("%Y-%m-%d")
        weights = learner.get_weights()
        scores  = learner.get_ensemble_scores()

        new_prob     = core.recommend_best(df, weights)
        new_value    = core.value_recommend(df, weights)
        new_strategy = core.strategy_recommend(df)
        new_ensemble = core.ensemble_recommend(df, weights, scores)

        _save_rec(latest_date, new_prob)
        _save_value_rec(latest_date, new_value)
        _save_strategy_rec(latest_date, new_strategy)

        # 迭代完成後備份
        backup_manager.send_backup_to_telegram()
    except Exception:
        import traceback
        print("539 daily learn error:", traceback.format_exc())


def _539_weekly_report():
    """每週一 09:05：539 週深度學習（靜默），週報併入 _weekly_evolution_report"""
    try:
        learner.weekly_deep_analysis()   # 學習 + 保存，不單獨推播
    except Exception:
        pass


def _weekly_evolution_report():
    """
    每週一 09:15：整合週進化報告 → TG 推播
    格式：本週結果 + 系統自動診斷 + 下週進化方向
    """
    try:
        import datetime as _dt

        # ── 取各系統本週數據 ──────────────────────────────────────
        b_summary  = bingo_learner.get_summary()
        l_summary  = learner.get_summary()
        b_history  = b_summary.get("history", [])[-7:]
        l_history  = l_summary.get("history", [])[-7:]
        sw         = b_summary.get("strategy_weights", {})

        avg6  = b_summary.get("avg_six_hits",  0)
        avg9  = b_summary.get("avg_nine_hits", 0)
        target6, target9 = 3.0, 4.0

        # 539 近7期平均命中
        prob_avg  = round(sum(r.get("prob_hits",     0) for r in l_history) / max(len(l_history), 1), 2)
        val_avg   = round(sum(r.get("value_hits",    0) for r in l_history) / max(len(l_history), 1), 2)
        strat_avg = round(sum(r.get("strategy_hits", 0) for r in l_history) / max(len(l_history), 1), 2)
        best_539  = max(prob_avg, val_avg, strat_avg)
        best_539_name = {prob_avg: "機率", val_avg: "價值", strat_avg: "策略"}[best_539]

        # ── 自動診斷 ──────────────────────────────────────────────
        diag_bingo = []
        if avg6 < 2.0:
            diag_bingo.append("6星嚴重低命中 → 下週加重 S3冷號策略比重")
        elif avg6 < target6:
            diag_bingo.append(f"6星命中 {avg6:.2f} 距目標差 {target6-avg6:.2f} → 維持現策略觀察")
        else:
            diag_bingo.append(f"✅ 6星已達目標 {avg6:.2f}！保持當前策略")

        if avg9 < 2.5:
            diag_bingo.append("9星嚴重低命中 → 下週加重 S1動能策略比重")
        elif avg9 < target9:
            diag_bingo.append(f"9星命中 {avg9:.2f} 距目標差 {target9-avg9:.2f} → 持續迭代")
        else:
            diag_bingo.append(f"✅ 9星已達目標 {avg9:.2f}！保持當前策略")

        # 策略投票權重排名
        sw_sorted = sorted(sw.items(), key=lambda x: x[1], reverse=True)
        sw_name = {"s1_momentum":"超短動能","s2_hot":"短期熱號","s3_cold":"冷號回歸",
                   "s4_cooccur":"強力共現","s5_zone":"區間峰值","s6_learner":"學習模型"}
        top_strategy   = sw_name.get(sw_sorted[0][0], sw_sorted[0][0]) if sw_sorted else "-"
        worst_strategy = sw_name.get(sw_sorted[-1][0], sw_sorted[-1][0]) if sw_sorted else "-"

        diag_539 = []
        if best_539 < 1.5:
            diag_539.append("539 命中嚴重不足 → 下週擴大學習衰減率")
        elif best_539 < 2.5:
            diag_539.append(f"539 最佳策略「{best_539_name}」{best_539:.2f}顆 → 加重其投票比重")
        else:
            diag_539.append(f"✅ 539「{best_539_name}」命中 {best_539:.2f}顆 表現佳")

        # ── 下週進化方向 ──────────────────────────────────────────
        next_actions = []

        # Bingo 進化
        if avg6 < 2.0 or avg9 < 2.5:
            next_actions.append("🔧 Bingo：本週命中不足，系統已自動調低劣策略投票權重")
        if avg6 >= target6 and avg9 >= target9:
            next_actions.append("🎯 Bingo：雙目標達成！策略維持現狀，持續收集數據驗證")
        else:
            next_actions.append(f"📈 Bingo：強化策略「{top_strategy}」(本週最佳)，削減「{worst_strategy}」比重")

        # 539 進化
        next_actions.append(f"📊 539：加重「{best_539_name}推薦」投票比重至下週")

        # Faker
        try:
            faker_s = prize_tracker.get_faker_learn_summary()
            f_avg   = faker_s.get("recent_avg_hits", 0)
            if f_avg < 3:
                next_actions.append(f"🃏 Faker：近期命中 {f_avg:.1f}/5，下週加強爬蟲更新頻率")
            else:
                next_actions.append(f"🃏 Faker：命中 {f_avg:.1f}/5 正常，持續迭代")
        except Exception:
            pass

        # ── 組裝報告 ──────────────────────────────────────────────
        week_str = _dt.datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
        lines = [
            f"📋 週進化報告 {week_str}",
            "═" * 36,
            "",
            "🎯 本週結果",
            f"  Bingo 6星：{avg6:.2f} 顆  (目標 {target6})",
            f"  Bingo 9星：{avg9:.2f} 顆  (目標 {target9})",
            f"  539 最佳命中：{best_539:.2f} 顆（{best_539_name}策略）",
            "",
            "🔍 系統診斷",
        ]
        for d in diag_bingo + diag_539:
            lines.append(f"  {d}")

        lines += [
            "",
            "⚙️  策略投票權重（已自動更新）",
        ]
        for k, v in sw_sorted[:3]:
            lines.append(f"  {sw_name.get(k,k)}：{v:.2f}")

        lines += [
            "",
            "🚀 下週進化方向",
        ]
        for a in next_actions:
            lines.append(f"  {a}")

        lines += [
            "",
            f"  累計學習：Bingo {b_summary.get('total_rounds',0)} 期 ／ 539 {l_summary.get('total_rounds',0)} 期",
            "═" * 36,
        ]

        _send_telegram("\n".join(lines))

    except Exception:
        import traceback
        print("weekly evolution report error:", traceback.format_exc())


scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Taipei"))
scheduler.add_job(_auto_update,           "cron", day_of_week="mon-sat", hour=21, minute=0)
scheduler.add_job(_539_daily_learn,       "cron", day_of_week="mon-sat", hour=21, minute=5)
scheduler.add_job(_539_weekly_report,        "cron", day_of_week="mon", hour=9, minute=5)
scheduler.add_job(_faker_weekly_deep_learn,  "cron", day_of_week="mon", hour=9, minute=10)
scheduler.add_job(_weekly_evolution_report,  "cron", day_of_week="mon", hour=9, minute=15)
scheduler.add_job(_bingo_auto_update,     "interval", minutes=5)
# 每日投注快照（12:00 / 16:00 / 20:00）
scheduler.add_job(_take_snapshot, "cron", hour=12, minute=0,  args=["12:00"])
scheduler.add_job(_take_snapshot, "cron", hour=16, minute=0,  args=["16:00"])
scheduler.add_job(_take_snapshot, "cron", hour=20, minute=0,  args=["20:00"])
# 00:00 Bingo 迭代學習（開獎 07:05~23:55 結束後）
scheduler.add_job(_bingo_midnight_learn,      "cron", hour=0,  minute=5)
scheduler.add_job(_bingo_weekly_deep_learn,   "cron", day_of_week="mon", hour=0, minute=10)
# Bingo 盤中即時學習：每2小時更新一次球號權重（追蹤盤中趨勢）
def _bingo_intraday_learn():
    """每2小時盤中即時更新 Bingo 球號權重"""
    try:
        df = bingo_core.load_data()
        if df is not None and not df.empty:
            bingo_learner.intraday_update(df)
    except Exception:
        pass

for _h in [2, 4, 6, 8, 10, 14, 16, 18, 20, 22]:   # 每2小時，避開快照時段
    scheduler.add_job(_bingo_intraday_learn, "cron", hour=_h, minute=30)
# 09:00 傳送每日 TG 報告
scheduler.add_job(_morning_routine,       "cron", hour=9, minute=0)
# 每日 12:00 傳送 Faker 推薦
scheduler.add_job(_faker_noon_report,     "cron", day_of_week="mon-sat", hour=12, minute=0)
# 22:00 傳送 539 中獎人數分析報告
scheduler.add_job(_prize_report_routine,  "cron", day_of_week="mon-sat", hour=22, minute=0)
# 每週一 00:05 快照週冠軍排行
scheduler.add_job(virtual_bingo.save_weekly_champion,      "cron", day_of_week="mon", hour=0, minute=5)
def _weekly_full_reset():
    virtual_bingo.weekly_reset_all_balances()
    castle_war.weekly_reset()

scheduler.add_job(_weekly_full_reset, "cron", day_of_week="mon", hour=7, minute=0)
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
