"""
虛擬 Bingo 投注平台
────────────────────────────────────────────
支援多組帳號（密碼登入），各 NT$10,000 初始資金

玩法（台彩官方）：
  基本玩法  1星～10星（每注 NT$25，可加倍 2～50 倍）
  超級獎號  猜第20顆開獎號（選1～20個號碼）
  猜大小    20顆中≥13顆在41-80=猜大，≥13顆在1-40=猜小
  猜單雙    20顆中≥13顆為單=猜單，≥13顆為雙=猜雙

連買幾期：同一注自動延續 N 期
每期開獎後自動對獎、更新餘額
"""

import json
import uuid
import hashlib
import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from itertools import combinations as _combs

import pytz

_TW       = pytz.timezone("Asia/Taipei")
_DATA_DIR = Path("/data") if Path("/data").exists() else Path(__file__).parent

USERS_FILE     = _DATA_DIR / "virtual_users.json"
BETS_FILE      = _DATA_DIR / "virtual_bets.json"
CHAMPIONS_FILE = _DATA_DIR / "weekly_champions.json"
CHAT_FILE      = _DATA_DIR / "virtual_chat.json"

CHAT_MAX_MSG   = 200   # 最多保留幾則訊息

UNIT_PRICE       = 25       # NT$25 每注（台彩官方）
STARTING_BALANCE = 10_000   # NT$10,000 初始資金

# ── 基本玩法賠率表（台彩官方）────────────────────────────────────────────────
# ODDS[選幾星][中幾星] = 倍率（win = 倍率 × 每注實際金額 NT$25×bet_mult）
# 資料來源：台灣彩券官方 https://www.taiwanlottery.com/lotto/info/bingo_bingo
ODDS: dict[int, dict[int, float]] = {
    1:  {1: 2},
    2:  {1: 1,    2: 3},
    3:  {2: 2,    3: 20},
    4:  {3: 4,    4: 40},
    5:  {4: 20,   5: 300},
    6:  {5: 40,   6: 1_000},
    7:  {7: 3_200},
    8:  {8: 20_000},
    9:  {8: 4_000,  9: 40_000},
    10: {0: 1, 5: 1, 6: 10, 7: 100, 8: 1_000, 9: 10_000, 10: 200_000},
}
# 各星說明：
#  1★ 中1 → NT$50     2★ 中2 → NT$75 / 中1退本NT$25
#  3★ 中3 → NT$500 / 中2 → NT$50
#  4★ 中4 → NT$1,000 / 中3 → NT$100
#  5★ 中5 → NT$7,500 / 中4 → NT$500
#  6★ 中6 → NT$25,000 / 中5 → NT$1,000
#  7★ 中7 → NT$80,000      8★ 中8 → NT$500,000
#  9★ 中9 → NT$1,000,000 / 中8 → NT$100,000
# 10★ 中10 → NT$5,000,000 / 中9 → NT$250,000 / 中8 → NT$25,000
#     中7 → NT$2,500 / 中6 → NT$250 / 中5 → NT$25 / 中0 → NT$25

# ── 超級獎號（固定賠率）──────────────────────────────────────────────────────
# 第20顆球為超級獎號；猜中 → 固定 ×48 = NT$1,200（每注 NT$25×bet_mult × 48）
# 不管選幾個號碼，每一注固定賠率相同
SUPER_ODDS = 48

# ── 猜大小 / 猜單雙賠率（台彩官方）─────────────────────────────────────────
# 猜中 → ×6 = NT$150（每注 NT$25×bet_mult × 6）
BIGSMALL_ODDS = 6
ODDEVEN_ODDS  = 6

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _tw_now() -> str:
    return datetime.now(_TW).strftime("%Y-%m-%d %H:%M:%S")

def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


# ── 帳號管理 ──────────────────────────────────────────────────────────────────

def _load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            pass
    return {"users": []}

def _save_users(data: dict):
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def _load_bets() -> dict:
    if BETS_FILE.exists():
        try:
            return json.loads(BETS_FILE.read_text())
        except Exception:
            pass
    return {"bets": []}

def _save_bets(data: dict):
    BETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def init_users(accounts: list) -> dict:
    """
    初始化帳號（首次設定，已存在的跳過）
    accounts = [{"id":"a1","name":"大家長","password":"pw123"}, ...]
    """
    data = _load_users()
    existing_ids = {u["id"] for u in data["users"]}
    created = []
    for acc in accounts:
        if acc["id"] not in existing_ids:
            data["users"].append({
                "id":            acc["id"],
                "name":          acc["name"],
                "password_hash": _hash_pw(acc["password"]),
                "balance":       STARTING_BALANCE,
                "total_bet":     0,
                "total_win":     0,
                "created_at":    _tw_now(),
                "is_admin":      acc.get("is_admin", False),
            })
            created.append(acc["id"])
    _save_users(data)
    return {"ok": True, "created": created, "total": len(data["users"])}


def get_user(user_id: str) -> dict | None:
    data = _load_users()
    return next((u for u in data["users"] if u["id"] == user_id), None)


def verify_login(user_id: str, password: str) -> dict | None:
    user = get_user(user_id)
    if user and user["password_hash"] == _hash_pw(password):
        return {k: v for k, v in user.items() if k != "password_hash"}
    return None


def change_name(user_id: str, new_name: str) -> dict:
    new_name = new_name.strip()
    if not new_name:
        return {"ok": False, "msg": "暱稱不可空白"}
    if len(new_name) > 20:
        return {"ok": False, "msg": "暱稱最多 20 個字"}
    data = _load_users()
    for u in data["users"]:
        if u["id"] == user_id:
            u["name"] = new_name
            _save_users(data)
            return {"ok": True, "name": new_name}
    return {"ok": False, "msg": "帳號不存在"}


def change_password(user_id: str, old_pw: str, new_pw: str) -> dict:
    data = _load_users()
    for u in data["users"]:
        if u["id"] == user_id:
            if u["password_hash"] != _hash_pw(old_pw):
                return {"ok": False, "msg": "舊密碼錯誤"}
            u["password_hash"] = _hash_pw(new_pw)
            _save_users(data)
            return {"ok": True}
    return {"ok": False, "msg": "帳號不存在"}


def get_leaderboard() -> list:
    data = _load_users()
    board = []
    for u in data["users"]:
        pnl = u["balance"] - STARTING_BALANCE
        roi = round(pnl / u["total_bet"] * 100, 2) if u["total_bet"] > 0 else 0
        board.append({
            "id":        u["id"],
            "name":      u["name"],
            "balance":   u["balance"],
            "pnl":       pnl,
            "total_bet": u["total_bet"],
            "total_win": u["total_win"],
            "roi":       roi,
        })
    board.sort(key=lambda x: x["balance"], reverse=True)
    return board


def reset_user_balance(user_id: str, amount: int = STARTING_BALANCE) -> dict:
    data = _load_users()
    for u in data["users"]:
        if u["id"] == user_id:
            u["balance"] = amount
            _save_users(data)
            return {"ok": True, "balance": amount}
    return {"ok": False, "msg": "帳號不存在"}


def list_users() -> list:
    data = _load_users()
    return [{k: v for k, v in u.items() if k != "password_hash"} for u in data["users"]]


# ── 玩法計算 ──────────────────────────────────────────────────────────────────

def calc_cost(bet_type: str, pick_count: int,
              balls: list = None, dan_balls: list = None, tuo_balls: list = None,
              multiplier: int = 1) -> dict:
    """
    計算注數與費用（含加倍）
    multiplier: 1~50 倍（每注 NT$25 × multiplier）
    Returns: {"valid":bool, "combinations":N, "unit_cost":N×25×mult, "cost":unit×repeats, "msg":""}
    """
    balls      = balls or []
    dan_balls  = dan_balls or []
    tuo_balls  = tuo_balls or []
    multiplier = max(1, min(50, int(multiplier)))

    # ── 基本玩法（1~10星）
    if bet_type in ("直玩", "複式", "膽拖", "快選"):
        if pick_count < 1 or pick_count > 10:
            return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": "選星數需在 1~10 之間"}

        if bet_type == "直玩":
            if len(balls) != pick_count:
                return {"valid": False, "combinations": 0, "unit_cost": 0,
                        "msg": f"直玩需選 {pick_count} 個號碼（目前 {len(balls)} 個）"}
            combs = 1

        elif bet_type == "複式":
            if len(balls) <= pick_count:
                return {"valid": False, "combinations": 0, "unit_cost": 0,
                        "msg": f"複式選號需多於 {pick_count}（目前 {len(balls)} 個）"}
            combs = math.comb(len(balls), pick_count)

        elif bet_type == "膽拖":
            if not dan_balls or not tuo_balls:
                return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": "請指定膽球和拖球"}
            need = pick_count - len(dan_balls)
            if need < 1:
                return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": "膽球數不可大於等於選星數"}
            if len(tuo_balls) < need:
                return {"valid": False, "combinations": 0, "unit_cost": 0,
                        "msg": f"拖球至少需 {need} 個（目前 {len(tuo_balls)} 個）"}
            if set(dan_balls) & set(tuo_balls):
                return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": "膽球與拖球不可重複"}
            combs = math.comb(len(tuo_balls), need)

        else:  # 快選
            combs = 1

    # ── 超級獎號
    elif bet_type == "超級獎號":
        if len(balls) < 1 or len(balls) > 20:
            return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": "超級獎號需選 1~20 個號碼"}
        combs = len(balls)   # 每選一個號碼 = 一注

    # ── 猜大小
    elif bet_type == "猜大小":
        if not balls:  # balls 用來傳 ["大"] / ["小"] / ["大","小"]
            return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": "請選擇猜大或猜小"}
        combs = len(balls)   # 猜大=1注、猜大+小=2注

    # ── 猜單雙
    elif bet_type == "猜單雙":
        if not balls:
            return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": "請選擇猜單或猜雙"}
        combs = len(balls)

    else:
        return {"valid": False, "combinations": 0, "unit_cost": 0, "msg": f"不支援的玩法: {bet_type}"}

    unit_cost = combs * UNIT_PRICE * multiplier
    return {"valid": True, "combinations": combs, "unit_cost": unit_cost, "multiplier": multiplier, "msg": ""}


# ── 下注 ──────────────────────────────────────────────────────────────────────

def place_bet(user_id: str, bet_type: str, pick_count: int,
              balls: list = None, dan_balls: list = None, tuo_balls: list = None,
              multiplier: int = 1, repeat_draws: int = 1, current_period: str = "") -> dict:
    """
    下注入口
    repeat_draws : 連買幾期（1~50）
    current_period: 從哪期開始
    """
    # 猜大小/猜單雙 的 balls 是字串清單（["大","小"] / ["單","雙"]），不做 int 轉換
    if bet_type in ("猜大小", "猜單雙"):
        balls     = list(dict.fromkeys(str(x) for x in (balls or [])))   # 去重保序
        dan_balls = []
        tuo_balls = []
    else:
        balls     = sorted(set(int(x) for x in (balls or [])))
        dan_balls = sorted(set(int(x) for x in (dan_balls or [])))
        tuo_balls = sorted(set(int(x) for x in (tuo_balls or [])))

    # 快選：隨機產生
    if bet_type == "快選":
        balls = sorted(random.sample(range(1, 81), pick_count))

    # 號碼範圍驗證（數字類型才驗）
    if bet_type not in ("猜大小", "猜單雙"):
        all_nums = balls + dan_balls + tuo_balls
        if any(n < 1 or n > 80 for n in all_nums):
            return {"ok": False, "msg": "號碼需在 1~80 之間"}

    # 加倍限制
    multiplier = max(1, min(50, int(multiplier)))

    # 連買限制
    repeat_draws = int(repeat_draws)
    if repeat_draws < 1 or repeat_draws > 50:
        return {"ok": False, "msg": "連買期數需在 1~50 之間"}

    # 計算費用
    cost_info = calc_cost(bet_type, pick_count, balls, dan_balls, tuo_balls, multiplier)
    if not cost_info["valid"]:
        return {"ok": False, "msg": cost_info["msg"]}

    total_cost = cost_info["unit_cost"] * repeat_draws

    # 扣款
    users_data = _load_users()
    user = next((u for u in users_data["users"] if u["id"] == user_id), None)
    if not user:
        return {"ok": False, "msg": "帳號不存在"}
    if user["balance"] < total_cost:
        return {"ok": False, "msg": f"餘額不足（需 NT${total_cost:,}，剩 NT${user['balance']:,}）"}

    user["balance"]   -= total_cost
    user["total_bet"] += total_cost
    _save_users(users_data)

    # 記錄投注
    bet_id = str(uuid.uuid4())[:8].upper()
    bet = {
        "id":              bet_id,
        "user_id":         user_id,
        "bet_type":        bet_type,
        "pick_count":      pick_count,
        "balls":           balls,
        "dan_balls":       dan_balls,
        "tuo_balls":       tuo_balls,
        "combinations":    cost_info["combinations"],
        "multiplier":      multiplier,
        "cost_per_draw":   cost_info["unit_cost"],
        "total_cost":      total_cost,
        "repeat_draws":    repeat_draws,
        "remaining_draws": repeat_draws,
        "status":          "pending",
        "created_at":      _tw_now(),
        "from_period":     current_period,
        "results":         [],
    }
    bets_data = _load_bets()
    bets_data["bets"].append(bet)
    _save_bets(bets_data)

    return {
        "ok":           True,
        "bet_id":       bet_id,
        "cost":         total_cost,
        "balance":      user["balance"],
        "combinations": cost_info["combinations"],
        "multiplier":   multiplier,
        "repeat_draws": repeat_draws,
        "balls":        balls or (dan_balls + tuo_balls),
    }


# ── 單注結算 ──────────────────────────────────────────────────────────────────

def _win_for_combo(pick_count: int, combo: list, drawn: list, unit: int = UNIT_PRICE) -> dict:
    hits       = len(set(combo) & set(drawn))
    multiplier = ODDS.get(pick_count, {}).get(hits, 0)
    win        = int(multiplier * unit)
    return {"hits": hits, "multiplier": multiplier, "win": win}


def _settle_one(bet: dict, period: str, drawn_balls: list, super_ball: int = None) -> dict:
    """
    結算單一投注的一期
    super_ball: 第20顆球（超級獎號用）
    """
    pc         = bet["pick_count"]
    bt         = bet["bet_type"]
    mult       = bet.get("multiplier", 1)
    unit       = UNIT_PRICE * mult   # 每注實際金額

    # ── 基本玩法 ──────────────────────────────────────────────────────────────
    if bt in ("直玩", "快選"):
        r = _win_for_combo(pc, bet["balls"], drawn_balls, unit)
        win_amount = r["win"]
        best_hits  = r["hits"]
        detail     = [{"balls": bet["balls"], "hits": r["hits"], "win": r["win"]}]

    elif bt == "複式":
        detail, win_amount, best_hits = [], 0, 0
        for combo in _combs(bet["balls"], pc):
            r = _win_for_combo(pc, list(combo), drawn_balls, unit)
            win_amount += r["win"]
            best_hits   = max(best_hits, r["hits"])
            if len(detail) < 10:
                detail.append({"balls": list(combo), "hits": r["hits"], "win": r["win"]})

    elif bt == "膽拖":
        need        = pc - len(bet["dan_balls"])
        detail, win_amount, best_hits = [], 0, 0
        for tuo_combo in _combs(bet["tuo_balls"], need):
            combo = bet["dan_balls"] + list(tuo_combo)
            r = _win_for_combo(pc, combo, drawn_balls, unit)
            win_amount += r["win"]
            best_hits   = max(best_hits, r["hits"])
            if len(detail) < 10:
                detail.append({"balls": combo, "hits": r["hits"], "win": r["win"]})

    # ── 超級獎號 ──────────────────────────────────────────────────────────────
    elif bt == "超級獎號":
        # 固定 ×48，不管選幾個號碼；第20顆球猜中即中獎（最多中1注）
        is_hit     = (super_ball is not None) and (super_ball in bet["balls"])
        win_amount = int(unit * SUPER_ODDS) if is_hit else 0
        best_hits  = 1 if is_hit else 0
        detail     = [{"super_ball": super_ball, "selected": bet["balls"],
                       "hit": is_hit, "win": win_amount}]

    # ── 猜大小 ────────────────────────────────────────────────────────────────
    elif bt == "猜大小":
        small_cnt = sum(1 for b in drawn_balls if b <= 40)
        big_cnt   = 20 - small_cnt
        win_amount = 0
        results_detail = []
        for choice in bet["balls"]:   # balls = ["大"] / ["小"] / ["大","小"]
            if choice == "大":
                hit = big_cnt >= 13
            else:
                hit = small_cnt >= 13
            w = int(unit * BIGSMALL_ODDS) if hit else 0
            win_amount += w
            results_detail.append({"choice": choice, "hit": hit, "win": w,
                                    "big_cnt": big_cnt, "small_cnt": small_cnt})
        best_hits = sum(1 for d in results_detail if d["hit"])
        detail    = results_detail

    # ── 猜單雙 ────────────────────────────────────────────────────────────────
    elif bt == "猜單雙":
        odd_cnt  = sum(1 for b in drawn_balls if b % 2 == 1)
        even_cnt = 20 - odd_cnt
        win_amount = 0
        results_detail = []
        for choice in bet["balls"]:   # balls = ["單"] / ["雙"] / ["單","雙"]
            if choice == "單":
                hit = odd_cnt >= 13
            else:
                hit = even_cnt >= 13
            w = int(unit * ODDEVEN_ODDS) if hit else 0
            win_amount += w
            results_detail.append({"choice": choice, "hit": hit, "win": w,
                                    "odd_cnt": odd_cnt, "even_cnt": even_cnt})
        best_hits = sum(1 for d in results_detail if d["hit"])
        detail    = results_detail

    else:
        win_amount, best_hits, detail = 0, 0, []

    return {
        "period":     period,
        "drawn":      drawn_balls[:20],
        "super_ball": super_ball,
        "hits":       best_hits,
        "win_amount": win_amount,
        "detail":     detail[:10],
        "settled_at": _tw_now(),
    }


# ── 批次結算 ──────────────────────────────────────────────────────────────────

def settle_period(period: str, drawn_balls: list, super_ball: int = None) -> dict:
    """
    結算某期所有 pending 投注。
    drawn_balls: 該期開出的 20 顆號碼（list of int）
    """
    bets_data  = _load_bets()
    users_data = _load_users()
    user_map   = {u["id"]: u for u in users_data["users"]}

    settled_count = 0
    total_paid    = 0

    for bet in bets_data["bets"]:
        if bet["status"] != "pending":
            continue
        if bet["remaining_draws"] <= 0:
            bet["status"] = "expired"
            continue
        # 避免同期重複結算
        already_periods = [r["period"] for r in bet["results"]]
        if period in already_periods:
            continue

        result = _settle_one(bet, period, drawn_balls, super_ball=super_ball)
        bet["results"].append(result)
        bet["remaining_draws"] -= 1
        if bet["remaining_draws"] <= 0:
            bet["status"] = "settled"

        if result["win_amount"] > 0:
            u = user_map.get(bet["user_id"])
            if u:
                u["balance"]   += result["win_amount"]
                u["total_win"] += result["win_amount"]
                total_paid     += result["win_amount"]

        settled_count += 1

    _save_bets(bets_data)
    _save_users(users_data)
    return {"period": period, "settled": settled_count, "total_paid": total_paid}


def auto_settle_from_df(df) -> dict:  # noqa
    """
    從 bingo_core DataFrame 取最新期次自動結算
    （由 _bingo_auto_update 每 5 分鐘呼叫）
    """
    if df is None or df.empty:
        return {"ok": False, "msg": "無開獎資料"}

    # 取最新一期
    latest = df.iloc[-1]
    period = str(int(float(latest["period"])))

    # 取出已開球（n1~n20，按數字排序，第20顆為超級獎號）
    ball_cols = sorted([c for c in df.columns if c.startswith("n") and c[1:].isdigit()],
                       key=lambda x: int(x[1:]))
    drawn = [int(latest[c]) for c in ball_cols if c in latest.index and latest[c] > 0]
    if not drawn:
        return {"ok": False, "msg": "無法取得開獎號碼"}

    super_ball = drawn[-1] if len(drawn) >= 20 else None  # 第20顆 = 超級獎號
    result = settle_period(period, drawn, super_ball=super_ball)
    return {"ok": True, **result}


# ── 查詢 ──────────────────────────────────────────────────────────────────────

def get_user_bets(user_id: str, limit: int = 50, status: str = None) -> list:
    data = _load_bets()
    bets = [b for b in data["bets"] if b["user_id"] == user_id]
    if status:
        bets = [b for b in bets if b["status"] == status]
    bets.sort(key=lambda b: b["created_at"], reverse=True)
    return bets[:limit]


def get_user_stats(user_id: str) -> dict:
    user = get_user(user_id)
    if not user:
        return {}
    bets = get_user_bets(user_id, limit=9999)
    total_periods = sum(len(b["results"]) for b in bets)
    win_periods   = sum(1 for b in bets for r in b["results"] if r["win_amount"] > 0)
    win_rate      = round(win_periods / total_periods * 100, 1) if total_periods else 0
    pending_cost  = sum(b["cost_per_draw"] * b["remaining_draws"]
                        for b in bets if b["status"] == "pending")
    return {
        "balance":       user["balance"],
        "pnl":           user["balance"] - STARTING_BALANCE,
        "total_bet":     user["total_bet"],
        "total_win":     user["total_win"],
        "total_bets":    len(bets),
        "pending_bets":  sum(1 for b in bets if b["status"] == "pending"),
        "pending_cost":  pending_cost,
        "win_rate":      win_rate,
        "win_periods":   win_periods,
        "total_periods": total_periods,
    }


def get_odds_table() -> dict:
    """回傳完整賠率表（供前端顯示）"""
    result = {}
    for pick, table in ODDS.items():
        result[str(pick)] = {str(hit): mult for hit, mult in table.items()}
    return result


# ── 每週冠軍快照 ──────────────────────────────────────────────────────────────

def _load_champions() -> dict:
    if CHAMPIONS_FILE.exists():
        try:
            return json.loads(CHAMPIONS_FILE.read_text())
        except Exception:
            pass
    return {"champions": []}

def _save_champions(data: dict):
    CHAMPIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def save_weekly_champion() -> dict:
    """
    快照當週排行榜第一名（含完整排行榜）
    每週一 00:05 由 APScheduler 呼叫
    """
    board = get_leaderboard()
    if not board:
        return {"ok": False, "msg": "無玩家資料"}

    now = datetime.now(_TW)
    # 上週（快照記錄的是剛結束的那一週）
    week_end   = (now - timedelta(days=1)).date()           # 上週日
    week_start = week_end - timedelta(days=6)               # 上週一
    iso_week   = week_start.strftime("%Y-W%W")              # e.g. 2026-W21

    # 補充每位玩家的勝率
    full_board = []
    for p in board:
        stats = get_user_stats(p["id"])
        full_board.append({**p, "win_rate": stats.get("win_rate", 0),
                           "total_periods": stats.get("total_periods", 0)})

    champion = full_board[0]

    data = _load_champions()
    # 避免同一週重複記錄
    existing_weeks = {c["week"] for c in data["champions"]}
    if iso_week in existing_weeks:
        return {"ok": False, "msg": f"本週 {iso_week} 已記錄"}

    record = {
        "week":         iso_week,
        "week_start":   str(week_start),
        "week_end":     str(week_end),
        "recorded_at":  _tw_now(),
        "champion":     champion,
        "leaderboard":  full_board,
    }
    data["champions"].insert(0, record)  # 最新在最前
    _save_champions(data)
    return {"ok": True, "week": iso_week, "champion": champion["name"]}


def get_weekly_champions(limit: int = 20) -> list:
    """取最近 N 週的冠軍記錄"""
    data = _load_champions()
    return data["champions"][:limit]


# ── 聊天室 ────────────────────────────────────────────────────────────────────

def _load_chat() -> dict:
    if CHAT_FILE.exists():
        try:
            return json.loads(CHAT_FILE.read_text())
        except Exception:
            pass
    return {"messages": []}

def _save_chat(data: dict):
    CHAT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# 表情符號白名單（快速回應按鈕用）
CHAT_REACTIONS = ["👍", "🎯", "🔥", "😂", "😮", "💰", "🍀", "😭"]

def send_chat(user_id: str, text: str, msg_type: str = "text") -> dict:
    """
    發送聊天訊息
    msg_type: "text" | "system"（系統通知）
    """
    text = text.strip()
    if not text:
        return {"ok": False, "msg": "訊息不可空白"}
    if len(text) > 200:
        return {"ok": False, "msg": "訊息最多 200 字"}

    user = get_user(user_id)
    if not user:
        return {"ok": False, "msg": "帳號不存在"}

    data = _load_chat()
    msg_id = str(uuid.uuid4())[:8].upper()
    msg = {
        "id":        msg_id,
        "user_id":   user_id,
        "name":      user["name"],
        "text":      text,
        "type":      msg_type,
        "reactions": {},   # {"👍": ["uid1","uid2"], ...}
        "created_at": _tw_now(),
    }
    data["messages"].append(msg)
    # 超過上限時裁切舊訊息
    if len(data["messages"]) > CHAT_MAX_MSG:
        data["messages"] = data["messages"][-CHAT_MAX_MSG:]
    _save_chat(data)
    return {"ok": True, "msg_id": msg_id}


def get_chat(limit: int = 50, since_id: str = None) -> list:
    """
    取最近訊息（新→舊），若指定 since_id 只回傳該 id 之後的新訊息
    """
    data = _load_chat()
    msgs = data["messages"]
    if since_id:
        ids = [m["id"] for m in msgs]
        if since_id in ids:
            idx = ids.index(since_id)
            msgs = msgs[idx+1:]
    return msgs[-limit:]   # 回傳最新 N 則


def add_reaction(user_id: str, msg_id: str, emoji: str) -> dict:
    """對某則訊息加/取消表情回應（toggle）"""
    if emoji not in CHAT_REACTIONS:
        return {"ok": False, "msg": "不支援的表情"}
    user = get_user(user_id)
    if not user:
        return {"ok": False, "msg": "帳號不存在"}
    data = _load_chat()
    for m in data["messages"]:
        if m["id"] == msg_id:
            m.setdefault("reactions", {})
            m["reactions"].setdefault(emoji, [])
            if user_id in m["reactions"][emoji]:
                m["reactions"][emoji].remove(user_id)   # 取消
            else:
                m["reactions"][emoji].append(user_id)   # 新增
            _save_chat(data)
            return {"ok": True, "reactions": m["reactions"]}
    return {"ok": False, "msg": "找不到訊息"}


def delete_chat_msg(user_id: str, msg_id: str) -> dict:
    """刪除自己的訊息（或管理員刪任意訊息）"""
    user = get_user(user_id)
    if not user:
        return {"ok": False, "msg": "帳號不存在"}
    data = _load_chat()
    for i, m in enumerate(data["messages"]):
        if m["id"] == msg_id:
            if m["user_id"] != user_id and not user.get("is_admin"):
                return {"ok": False, "msg": "只能刪自己的訊息"}
            data["messages"].pop(i)
            _save_chat(data)
            return {"ok": True}
    return {"ok": False, "msg": "找不到訊息"}
