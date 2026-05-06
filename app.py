"""
今彩539 Web App — Flask 後端
"""

import json
import threading
from flask import Flask, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import core
import learner
import bingo_core
import bingo_tracker

app = Flask(__name__)

# 全量抓取進度
_fetch_state = {"running": False, "page": 0, "total": core.TOTAL_PAGES, "error": ""}

# 本期推薦快取：格式 {"draw_date": "2026-05-02", "best": [1,2,3,4,5]}
# 同一期內不論重新整理幾次，都回傳同一組號碼
_rec_file = core.DATA_FILE.parent / "current_rec.json"  # 與 core.DATA_FILE 同在 /data


def _load_rec() -> dict:
    if _rec_file.exists():
        try:
            return json.loads(_rec_file.read_text())
        except Exception:
            pass
    return {}


def _save_rec(draw_date: str, nums: list):
    _rec_file.write_text(json.dumps({"draw_date": draw_date, "best": nums}))


def _get_or_generate_rec(df) -> list[int]:
    """
    若快取的推薦與最新開獎日期相同，直接回傳快取。
    否則（新一期開始）重新產生並存檔。
    """
    latest_date = df["date"].max().strftime("%Y-%m-%d")
    cached = _load_rec()

    if cached.get("draw_date") == latest_date:
        return cached["best"]

    # 新一期：先對上期推薦做學習，再產生新推薦
    old_best = cached.get("best", [])
    if old_best:
        learner.auto_update_from_df(df, old_best, old_best)

    weights = learner.get_weights()
    best = core.recommend_best(df, weights)
    _save_rec(latest_date, best)
    return best


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

    return jsonify({
        "ok":        True,
        "best":      best,
        "draw_date": cached.get("draw_date", ""),
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
        if updated:
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
    """在指定時段擷取推薦並記錄投注快照"""
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            return
        pick = bingo_core.smart_pick(df)
        latest_period = str(int(df["period"].astype(float).max()))
        bingo_tracker.save_snapshot(slot_label, pick["six"], pick["nine"], latest_period)
    except Exception:
        pass


def _send_morning_report():
    """早上9點產生並寄出每日報告"""
    try:
        df = bingo_core.load_data()
        if df is None or df.empty:
            return
        html = bingo_tracker.daily_report_html(df)
        bingo_tracker.send_daily_email(html)
    except Exception:
        pass


scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Taipei"))
scheduler.add_job(_auto_update,       "cron", hour=21, minute=30)
scheduler.add_job(_bingo_auto_update, "interval", minutes=5)
# 每日投注快照
scheduler.add_job(_take_snapshot, "cron", hour=12, minute=0,  args=["12:00"])
scheduler.add_job(_take_snapshot, "cron", hour=16, minute=0,  args=["16:00"])
scheduler.add_job(_take_snapshot, "cron", hour=20, minute=0,  args=["20:00"])
# 每日早上9點報告
scheduler.add_job(_send_morning_report, "cron", hour=9, minute=0)
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=5539)
