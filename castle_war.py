"""
城池大戰 - 虛擬投注附屬小遊戲
玩家透過下注累積點數，招募部隊攻打其他玩家的城池。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
import pytz

_TW = pytz.timezone("Asia/Taipei")
_DATA_DIR   = Path("/data") if Path("/data").exists() else Path(__file__).parent
CASTLE_FILE = _DATA_DIR / "castle_war.json"

# ── 常數 ──────────────────────────────────────────────────────────────────────
MAX_HP      = 500
FALLEN_HP   = 100       # 城池陷落後重置 HP
FALLEN_BAN  = 6         # 陷落後禁止攻擊幾小時
BANKRUPT_DMG= 150       # 破產懲罰傷害
BANKRUPT_BAN= 6         # 破產後禁止攻擊幾小時

# 部隊 {key: {name, cost, atk, pierce}}
# pierce = 穿甲比例（忽略守方幾 % 防禦）
UNITS = {
    "soldier":  {"name": "小兵",     "cost": 10,  "atk": 25,  "pierce": 0.0,  "emoji": "🗡️"},
    "elite":    {"name": "強化小兵", "cost": 30,  "atk": 80,  "pierce": 0.1,  "emoji": "⚔️"},
    "catapult": {"name": "投石車",   "cost": 80,  "atk": 200, "pierce": 0.5,  "emoji": "💣"},
}
WALL_COST    = 20    # 每級城牆費用
WALL_DEF     = 25    # 每級城牆防禦值
MAX_WALL_LVL = 4
SHIELD_COST  = 50    # 護盾費用
FALLEN_BONUS = 150   # 攻下城池獎勵點數
BATTLE_LOG_MAX = 50  # 保留最多幾筆戰役記錄

def _tw_now() -> str:
    return datetime.now(_TW).strftime("%Y-%m-%d %H:%M:%S")

def _load() -> dict:
    if CASTLE_FILE.exists():
        try:
            return json.loads(CASTLE_FILE.read_text())
        except Exception:
            pass
    return {"castles": {}, "battle_log": []}

def _save(data: dict):
    tmp = CASTLE_FILE.with_suffix(".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text)
    tmp.replace(CASTLE_FILE)

def _default_castle(user_id: str, name: str) -> dict:
    return {
        "user_id":       user_id,
        "name":          name,
        "hp":            MAX_HP,
        "wall_level":    0,
        "has_shield":    False,
        "points":        0,
        "army":          {"soldier": 0, "elite": 0, "catapult": 0},
        "ban_until":     None,   # 禁止攻擊到這個時間
        "fallen_at":     None,   # 上次陷落時間
        "total_attacks": 0,
        "total_fallen":  0,
        "created_at":    _tw_now(),
    }

def _is_banned(castle: dict) -> tuple[bool, str]:
    """回傳 (is_banned, reason)"""
    if castle.get("ban_until"):
        ban_dt = datetime.fromisoformat(castle["ban_until"])
        if ban_dt.tzinfo is None:
            ban_dt = _TW.localize(ban_dt)
        now = datetime.now(_TW)
        if now < ban_dt:
            remain = int((ban_dt - now).total_seconds() / 60)
            return True, f"禁止行動中（剩 {remain} 分鐘）"
    return False, ""

def ensure_castle(user_id: str, name: str) -> dict:
    """確保玩家有城池，沒有則初始化"""
    data = _load()
    if user_id not in data["castles"]:
        data["castles"][user_id] = _default_castle(user_id, name)
        _save(data)
    return data["castles"][user_id]

# ── 點數獲取 ──────────────────────────────────────────────────────────────────
def calc_bet_points(repeat_draws: int) -> int:
    """
    根據投注期數計算獲得點數：
    1期=10點，10期=120點（+20獎勵），20期=250點（+50獎勵），超過上限無額外獎勵
    """
    if repeat_draws <= 0:
        return 0
    if repeat_draws >= 20:
        return 250
    if repeat_draws >= 10:
        # 線性插值：10期120點 → 20期250點，每期+13
        return 120 + (repeat_draws - 10) * 13
    return repeat_draws * 10

def earn_points(user_id: str, name: str, repeat_draws: int) -> dict:
    """下注時呼叫，累積城池點數"""
    pts = calc_bet_points(repeat_draws)
    if pts <= 0:
        return {"ok": True, "points_earned": 0}
    data = _load()
    if user_id not in data["castles"]:
        data["castles"][user_id] = _default_castle(user_id, name)
    data["castles"][user_id]["points"] += pts
    _save(data)
    return {"ok": True, "points_earned": pts,
            "total_points": data["castles"][user_id]["points"]}

# ── 招募部隊 ──────────────────────────────────────────────────────────────────
def recruit(user_id: str, unit_key: str, count: int) -> dict:
    if unit_key not in UNITS:
        return {"ok": False, "msg": "不存在的部隊類型"}
    if count <= 0:
        return {"ok": False, "msg": "數量錯誤"}
    data = _load()
    c = data["castles"].get(user_id)
    if not c:
        return {"ok": False, "msg": "尚未初始化城池"}
    unit = UNITS[unit_key]
    cost = unit["cost"] * count
    if c["points"] < cost:
        return {"ok": False, "msg": f"點數不足（需 {cost}，擁有 {c['points']}）"}
    c["points"] -= cost
    c["army"][unit_key] = c["army"].get(unit_key, 0) + count
    _save(data)
    return {"ok": True, "recruited": count, "unit": unit["name"],
            "remaining_points": c["points"], "army": c["army"]}

# ── 城牆升級 ──────────────────────────────────────────────────────────────────
def upgrade_wall(user_id: str) -> dict:
    data = _load()
    c = data["castles"].get(user_id)
    if not c:
        return {"ok": False, "msg": "尚未初始化城池"}
    if c["wall_level"] >= MAX_WALL_LVL:
        return {"ok": False, "msg": f"城牆已達最高等級（{MAX_WALL_LVL}級）"}
    if c["points"] < WALL_COST:
        return {"ok": False, "msg": f"點數不足（需 {WALL_COST}，擁有 {c['points']}）"}
    c["points"] -= WALL_COST
    c["wall_level"] += 1
    _save(data)
    defense = c["wall_level"] * WALL_DEF
    return {"ok": True, "wall_level": c["wall_level"], "defense": defense,
            "remaining_points": c["points"]}

# ── 購買護盾 ──────────────────────────────────────────────────────────────────
def buy_shield(user_id: str) -> dict:
    data = _load()
    c = data["castles"].get(user_id)
    if not c:
        return {"ok": False, "msg": "尚未初始化城池"}
    if c.get("has_shield"):
        return {"ok": False, "msg": "護盾已啟用"}
    if c["points"] < SHIELD_COST:
        return {"ok": False, "msg": f"點數不足（需 {SHIELD_COST}，擁有 {c['points']}）"}
    c["points"] -= SHIELD_COST
    c["has_shield"] = True
    _save(data)
    return {"ok": True, "remaining_points": c["points"]}

# ── 攻擊 ──────────────────────────────────────────────────────────────────────
def attack(attacker_id: str, defender_id: str,
           army: dict) -> dict:
    """
    army = {"soldier": n, "elite": n, "catapult": n}
    """
    if attacker_id == defender_id:
        return {"ok": False, "msg": "不能攻打自己"}

    data = _load()
    atk_c = data["castles"].get(attacker_id)
    def_c = data["castles"].get(defender_id)
    if not atk_c or not def_c:
        return {"ok": False, "msg": "城池不存在"}

    # 檢查禁令
    banned, ban_msg = _is_banned(atk_c)
    if banned:
        return {"ok": False, "msg": ban_msg}

    # 驗證部隊數量
    total_units = 0
    for uk, cnt in army.items():
        cnt = int(cnt)
        if cnt < 0:
            return {"ok": False, "msg": "數量不能為負"}
        if atk_c["army"].get(uk, 0) < cnt:
            return {"ok": False, "msg": f"{UNITS[uk]['name']} 數量不足"}
        total_units += cnt
    if total_units == 0:
        return {"ok": False, "msg": "至少派出一隊部隊"}

    # 計算傷害
    def_level = def_c.get("wall_level", 0)
    base_def   = def_level * WALL_DEF   # 防禦值

    total_dmg = 0
    for uk, cnt in army.items():
        cnt = int(cnt)
        if cnt == 0:
            continue
        u = UNITS[uk]
        # 每單位傷害 = ATK × (1 - pierce × base_def/100) - base_def×(1-pierce)
        # 簡化：實際傷害 = ATK - base_def × (1 - pierce)
        effective_def = base_def * (1 - u["pierce"])
        unit_dmg = max(5, u["atk"] - effective_def) * cnt
        total_dmg += unit_dmg

    total_dmg = int(total_dmg)

    # 護盾攔截
    shielded = False
    if def_c.get("has_shield"):
        shielded = True
        total_dmg = 0
        def_c["has_shield"] = False

    # 扣血
    old_hp = def_c["hp"]
    def_c["hp"] = max(0, def_c["hp"] - total_dmg)

    # 消耗攻方部隊
    for uk, cnt in army.items():
        cnt = int(cnt)
        atk_c["army"][uk] = max(0, atk_c["army"].get(uk, 0) - cnt)

    atk_c["total_attacks"] = atk_c.get("total_attacks", 0) + 1

    # 陷落判定
    fallen = False
    bonus_pts = 0
    if def_c["hp"] == 0 and not shielded:
        fallen = True
        def_c["hp"] = FALLEN_HP
        def_c["wall_level"] = 0
        def_c["army"]    = {"soldier": 0, "elite": 0, "catapult": 0}
        def_c["has_shield"] = False
        ban_time = datetime.now(_TW) + timedelta(hours=FALLEN_BAN)
        def_c["ban_until"] = ban_time.strftime("%Y-%m-%d %H:%M:%S")
        def_c["fallen_at"] = _tw_now()
        def_c["total_fallen"] = def_c.get("total_fallen", 0) + 1
        # 攻方獲得獎勵點數
        atk_c["points"] = atk_c.get("points", 0) + FALLEN_BONUS
        bonus_pts = FALLEN_BONUS

    # 戰役記錄
    log_entry = {
        "time":          _tw_now(),
        "attacker":      atk_c["name"],
        "attacker_id":   attacker_id,
        "defender":      def_c["name"],
        "defender_id":   defender_id,
        "army_sent":     {k: v for k, v in army.items() if int(v) > 0},
        "damage":        total_dmg,
        "shielded":      shielded,
        "fallen":        fallen,
        "hp_before":     old_hp,
        "hp_after":      def_c["hp"],
        "bonus_pts":     bonus_pts,
    }
    data["battle_log"].insert(0, log_entry)
    data["battle_log"] = data["battle_log"][:BATTLE_LOG_MAX]

    _save(data)

    return {
        "ok":        True,
        "damage":    total_dmg,
        "shielded":  shielded,
        "fallen":    fallen,
        "defender_hp": def_c["hp"],
        "bonus_pts": bonus_pts,
    }

# ── 破產懲罰 ──────────────────────────────────────────────────────────────────
def apply_bankrupt_penalty(user_id: str) -> dict:
    """本金歸零時呼叫"""
    data = _load()
    c = data["castles"].get(user_id)
    if not c:
        return {"ok": False}
    c["hp"] = max(0, c["hp"] - BANKRUPT_DMG)
    c["army"] = {"soldier": 0, "elite": 0, "catapult": 0}
    ban_time = datetime.now(_TW) + timedelta(hours=BANKRUPT_BAN)
    c["ban_until"] = ban_time.strftime("%Y-%m-%d %H:%M:%S")

    log_entry = {
        "time":        _tw_now(),
        "attacker":    "💀 破產",
        "attacker_id": "__system__",
        "defender":    c["name"],
        "defender_id": user_id,
        "army_sent":   {},
        "damage":      BANKRUPT_DMG,
        "shielded":    False,
        "fallen":      c["hp"] == 0,
        "hp_before":   c["hp"] + BANKRUPT_DMG,
        "hp_after":    c["hp"],
        "bonus_pts":   0,
    }
    data["battle_log"].insert(0, log_entry)
    data["battle_log"] = data["battle_log"][:BATTLE_LOG_MAX]
    _save(data)
    return {"ok": True, "hp": c["hp"]}

# ── 週重置 ──────────────────────────────────────────────────────────────────
def weekly_reset() -> dict:
    """週一 07:00 與本金重置同步執行"""
    data = _load()
    for uid, c in data["castles"].items():
        c["hp"]         = MAX_HP
        c["wall_level"] = 0
        c["has_shield"] = False
        c["points"]     = 0
        c["army"]       = {"soldier": 0, "elite": 0, "catapult": 0}
        c["ban_until"]  = None
        c["fallen_at"]  = None
    data["battle_log"] = []   # 清空戰役記錄
    _save(data)
    return {"ok": True, "reset_at": _tw_now()}

# ── 查詢 ────────────────────────────────────────────────────────────────────
def get_all_castles() -> list:
    data = _load()
    result = []
    for uid, c in data["castles"].items():
        banned, ban_msg = _is_banned(c)
        defense = c.get("wall_level", 0) * WALL_DEF
        result.append({**c, "defense": defense, "is_banned": banned, "ban_msg": ban_msg})
    return result

def get_battle_log(limit: int = 20) -> list:
    data = _load()
    return data["battle_log"][:limit]

def get_castle(user_id: str) -> dict | None:
    data = _load()
    c = data["castles"].get(user_id)
    if not c:
        return None
    banned, ban_msg = _is_banned(c)
    defense = c.get("wall_level", 0) * WALL_DEF
    return {**c, "defense": defense, "is_banned": banned, "ban_msg": ban_msg}
