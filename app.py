"""
今彩539 Web App — Flask 後端
"""

import json
import threading
from flask import Flask, jsonify, render_template, request
import core
import learner

app = Flask(__name__)

# 全量抓取進度
_fetch_state = {"running": False, "page": 0, "total": core.TOTAL_PAGES, "error": ""}

# 記憶上一期推薦，供下次對獎使用
_last_rec_file = core.DATA_FILE.parent / "last_rec.json"

def _save_last_rec(prob: list, value: list):
    _last_rec_file.write_text(json.dumps({"prob": prob, "value": value}))

def _load_last_rec() -> tuple[list, list]:
    if _last_rec_file.exists():
        try:
            d = json.loads(_last_rec_file.read_text())
            return d.get("prob", []), d.get("value", [])
        except Exception:
            pass
    return [], []


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

        # 若有新一期開獎，自動對上期推薦
        check_result = None
        if updated:
            last_prob, last_value = _load_last_rec()
            check_result = learner.auto_update_from_df(df, last_prob, last_value)

        return jsonify({
            "ok":           True,
            "updated":      updated,
            "total":        len(df),
            "check_result": check_result,
        })
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

    weights = learner.get_weights()
    best    = core.recommend_best(df, weights)

    _save_last_rec(best, best)
    return jsonify({"ok": True, "best": best})


@app.route("/api/learn/history")
def api_learn_history():
    return jsonify(learner.get_summary())


if __name__ == "__main__":
    # 綁定 0.0.0.0 → 同網路其他裝置可連線
    app.run(host="0.0.0.0", debug=False, port=5539)
