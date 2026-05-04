"""
今彩539 Web App — Flask 後端
"""

import json
import threading
from flask import Flask, jsonify, render_template
import core
import learner

app = Flask(__name__)

# 全量抓取進度
_fetch_state = {"running": False, "page": 0, "total": core.TOTAL_PAGES, "error": ""}

# 本期推薦快取：格式 {"draw_date": "2026-05-02", "best": [1,2,3,4,5]}
# 同一期內不論重新整理幾次，都回傳同一組號碼
_rec_file = core.DATA_FILE.parent / "current_rec.json"


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=5539)
