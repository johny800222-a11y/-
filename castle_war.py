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
MAX_HP       = 500
FALLEN_HP    = 100
FALLEN_BAN   = 6       # 陷落後禁止行動幾小時
BANKRUPT_DMG = 150
BANKRUPT_BAN = 6

# 部隊設定
# atk_base   = 城內無守軍時的攻擊力
# atk_resist = 城內有對應守軍時的攻擊力（守軍攔截）
# cd_per_unit = 每招募一隻冷卻秒數
UNITS = {
    "soldier":  {
        "name": "小兵",     "cost": 10,  "emoji": "🗡️",
        "atk_base": 25,  "atk_resist": 10,
        "cd_per_unit": 270,   # 1隻=5min, 10隻=45min
    },
    "elite":    {
        "name": "強化小兵", "cost": 30,  "emoji": "⚔️",
        "atk_base": 80,  "atk_resist": 32,
        "cd_per_unit": 540,   # 1隻=9min, 10隻=90min
    },
    "catapult": {
        "name": "投石車",   "cost": 80,  "emoji": "💣",
        "atk_base": 100, "atk_resist": 40,
        "cd_per_unit": 1620,  # 1隻=27min, 10隻=270min
    },
}

WALL_COST     = 20
WALL_DEF      = 25
MAX_WALL_LVL  = 4
SHIELD_COST   = 50
SHIELD_CD_SEC = 1800   # 護盾冷卻 30 分鐘
FALLEN_BONUS  = 150
BATTLE_LOG_MAX= 50


def _tw_now() -> str:
    return datetime.now(_TW).strftime("%Y-%m-%d %H:%M:%S")

def _dt(s: str | None):
    """將字串轉成帶時區 datetime；None 或空字串回傳 None"""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return _TW.localize(dt) if dt.tzinfo is None else dt

def _load() -> dict:
    if CASTLE_FILE.exists():
        try:
            return json.loads(CASTLE_FILE.read_text())
        except Exception:
            pass
    return {"castles": {}, "battle_log": []}

def _save(data: dict):
    tmp = CASTLE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(CASTLE_FILE)

def _default_castle(user_id: str, name: str) -> dict:
    return {
        "user_id":         user_id,
        "name":            name,
        "hp":              MAX_HP,
        "wall_level":      0,
        "has_shield":      False,
        "points":          0,
        "army":            {"soldier": 0, "elite": 0, "catapult": 0},
        "ban_until":       None,
        "fallen_at":       None,
        # 招募冷卻（ISO 字串，到期時間）
        "recruit_cd":      {"soldier": None, "elite": None, "catapult": None},
        # 護盾冷卻
        "shield_cd_until": None,
        "total_attacks":   0,
        "total_fallen":    0,
        "created_at":      _tw_now(),
    }

def _is_banned(castle: dict) -> tuple[bool, str]:
    ban_dt = _dt(castle.get("ban_until"))
    if ban_dt and datetime.now(_TW) < ban_dt:
        remain = int((ban_dt - datetime.now(_TW)).total_seconds() / 60)
        return True, f"禁止行動中（剩 {remain} 分鐘）"
    return False, ""

def _remaining_cd_sec(iso_str: str | None) -> int:
    """回傳距冷卻結束的秒數（已結束則 0）"""
    dt = _dt(iso_str)
    if not dt:
        return 0
    remain = (dt - datetime.now(_TW)).total_seconds()
    return max(0, int(remain))

def _cd_info(castle: dict) -> dict:
    """計算各部隊與護盾的剩餘冷卻秒數"""
    rcd = castle.get("recruit_cd", {})
    return {
        "recruit_cd": {
            "soldier":  _remaining_cd_sec(rcd.get("soldier")),
            "elite":    _remaining_cd_sec(rcd.get("elite")),
            "catapult": _remaining_cd_sec(rcd.get("catapult")),
        },
        "shield_cd": _remaining_cd_sec(castle.get("shield_cd_until")),
    }

# ── 初始化 ────────────────────────────────────────────────────────────────────
def ensure_castle(user_id: str, name: str) -> dict:
    data = _load()
    if user_id not in data["castles"]:
        data["castles"][user_id] = _default_castle(user_id, name)
        _save(data)
    else:
        # 補上新欄位（舊資料相容）
        c = data["castles"][user_id]
        c.setdefault("recruit_cd", {"soldier": None, "elite": None, "catapult": None})
        c.setdefault("shield_cd_until", None)
        _save(data)
    return data["castles"][user_id]

# ── 點數 ──────────────────────────────────────────────────────────────────────
def calc_bet_points(repeat_draws: int) -> int:
    if repeat_draws <= 0:  return 0
    if repeat_draws >= 20: return 250
    if repeat_draws >= 10: return 120 + (repeat_draws - 10) * 13
    return repeat_draws * 10

def earn_points(user_id: str, name: str, repeat_draws: int) -> dict:
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

    # 冷卻檢查
    rcd = c.setdefault("recruit_cd", {"soldier": None, "elite": None, "catapult": None})
    remain = _remaining_cd_sec(rcd.get(unit_key))
    if remain > 0:
        m, s = divmod(remain, 60)
        return {"ok": False, "msg": f"冷卻中（剩 {m}分{s:02d}秒）"}

    unit = UNITS[unit_key]
    cost = unit["cost"] * count
    if c["points"] < cost:
        return {"ok": False, "msg": f"點數不足（需 {cost}，擁有 {c['points']}）"}

    c["points"] -= cost
    c["army"][unit_key] = c["army"].get(unit_key, 0) + count

    # 設定冷卻
    cd_sec = unit["cd_per_unit"] * count
    cd_until = datetime.now(_TW) + timedelta(seconds=cd_sec)
    rcd[unit_key] = cd_until.strftime("%Y-%m-%d %H:%M:%S")

    _save(data)
    return {
        "ok": True, "recruited": count, "unit": unit["name"],
        "remaining_points": c["points"], "army": c["army"],
        "cd_sec": cd_sec,
    }

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
    return {"ok": True, "wall_level": c["wall_level"],
            "defense": c["wall_level"] * WALL_DEF, "remaining_points": c["points"]}

# ── 購買護盾 ──────────────────────────────────────────────────────────────────
def buy_shield(user_id: str) -> dict:
    data = _load()
    c = data["castles"].get(user_id)
    if not c:
        return {"ok": False, "msg": "尚未初始化城池"}
    if c.get("has_shield"):
        return {"ok": False, "msg": "護盾已啟用"}

    # 冷卻檢查
    remain = _remaining_cd_sec(c.get("shield_cd_until"))
    if remain > 0:
        m, s = divmod(remain, 60)
        return {"ok": False, "msg": f"護盾冷卻中（剩 {m}分{s:02d}秒）"}

    if c["points"] < SHIELD_COST:
        return {"ok": False, "msg": f"點數不足（需 {SHIELD_COST}，擁有 {c['points']}）"}
    c["points"] -= SHIELD_COST
    c["has_shield"] = True

    cd_until = datetime.now(_TW) + timedelta(seconds=SHIELD_CD_SEC)
    c["shield_cd_until"] = cd_until.strftime("%Y-%m-%d %H:%M:%S")

    _save(data)
    return {"ok": True, "remaining_points": c["points"]}

# ── 攻擊 ──────────────────────────────────────────────────────────────────────
def attack(attacker_id: str, defender_id: str, army: dict) -> dict:
    if attacker_id == defender_id:
        return {"ok": False, "msg": "不能攻打自己"}

    data = _load()
    atk_c = data["castles"].get(attacker_id)
    def_c = data["castles"].get(defender_id)
    if not atk_c or not def_c:
        return {"ok": False, "msg": "城池不存在"}

    banned, ban_msg = _is_banned(atk_c)
    if banned:
        return {"ok": False, "msg": ban_msg}

    # 驗證部隊
    total_units = 0
    parsed = {}
    for uk in ("soldier", "elite", "catapult"):
        cnt = int(army.get(uk, 0))
        if cnt < 0:
            return {"ok": False, "msg": "數量不能為負"}
        if atk_c["army"].get(uk, 0) < cnt:
            return {"ok": False, "msg": f"{UNITS[uk]['name']} 數量不足"}
        parsed[uk] = cnt
        total_units += cnt
    if total_units == 0:
        return {"ok": False, "msg": "至少派出一隊部隊"}

    def_army = def_c.get("army", {"soldier":0,"elite":0,"catapult":0})

    # ── 新傷害機制 ────────────────────────────────────────────────
    # 每種兵：城內有對應守軍 → 低傷害；無守軍 → 高傷害
    # 守軍在被攻打後全部消失
    total_dmg = 0
    for uk in ("soldier", "elite", "catapult"):
        cnt = parsed[uk]
        if cnt == 0:
            continue
        u = UNITS[uk]
        has_defenders = def_army.get(uk, 0) > 0
        atk_per = u["atk_resist"] if has_defenders else u["atk_base"]
        total_dmg += atk_per * cnt

    # 城牆減傷（每級 -25 傷害）
    wall_reduction = def_c.get("wall_level", 0) * WALL_DEF
    total_dmg = max(0, total_dmg - wall_reduction)
    total_dmg = int(total_dmg)

    # 護盾攔截
    shielded = False
    if def_c.get("has_shield"):
        shielded = True
        total_dmg = 0
        def_c["has_shield"] = False

    old_hp = def_c["hp"]

    if not shielded:
        # 扣血
        def_c["hp"] = max(0, def_c["hp"] - total_dmg)
        # 守方部隊被攻打後全部消失
        def_c["army"] = {"soldier": 0, "elite": 0, "catapult": 0}

    # 消耗攻方部隊
    for uk, cnt in parsed.items():
        atk_c["army"][uk] = max(0, atk_c["army"].get(uk, 0) - cnt)

    atk_c["total_attacks"] = atk_c.get("total_attacks", 0) + 1

    # 陷落判定
    fallen = False
    bonus_pts = 0
    if def_c["hp"] == 0 and not shielded:
        fallen = True
        def_c["hp"] = FALLEN_HP
        def_c["wall_level"] = 0
        def_c["has_shield"] = False
        ban_time = datetime.now(_TW) + timedelta(hours=FALLEN_BAN)
        def_c["ban_until"] = ban_time.strftime("%Y-%m-%d %H:%M:%S")
        def_c["fallen_at"] = _tw_now()
        def_c["total_fallen"] = def_c.get("total_fallen", 0) + 1
        atk_c["points"] = atk_c.get("points", 0) + FALLEN_BONUS
        bonus_pts = FALLEN_BONUS

    # 戰役記錄
    log_entry = {
        "time":        _tw_now(),
        "attacker":    atk_c["name"],
        "attacker_id": attacker_id,
        "defender":    def_c["name"],
        "defender_id": defender_id,
        "army_sent":   {k: v for k, v in parsed.items() if v > 0},
        "damage":      total_dmg,
        "shielded":    shielded,
        "fallen":      fallen,
        "hp_before":   old_hp,
        "hp_after":    def_c["hp"],
        "bonus_pts":   bonus_pts,
    }
    data["battle_log"].insert(0, log_entry)
    data["battle_log"] = data["battle_log"][:BATTLE_LOG_MAX]
    _save(data)

    return {
        "ok": True, "damage": total_dmg,
        "shielded": shielded, "fallen": fallen,
        "defender_hp": def_c["hp"], "bonus_pts": bonus_pts,
    }

# ── 破產懲罰 ──────────────────────────────────────────────────────────────────
def apply_bankrupt_penalty(user_id: str) -> dict:
    data = _load()
    c = data["castles"].get(user_id)
    if not c:
        return {"ok": False}
    c["hp"] = max(0, c["hp"] - BANKRUPT_DMG)
    c["army"] = {"soldier": 0, "elite": 0, "catapult": 0}
    ban_time = datetime.now(_TW) + timedelta(hours=BANKRUPT_BAN)
    c["ban_until"] = ban_time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = {
        "time": _tw_now(), "attacker": "💀 破產", "attacker_id": "__system__",
        "defender": c["name"], "defender_id": user_id, "army_sent": {},
        "damage": BANKRUPT_DMG, "shielded": False, "fallen": c["hp"]==0,
        "hp_before": c["hp"]+BANKRUPT_DMG, "hp_after": c["hp"], "bonus_pts": 0,
    }
    data["battle_log"].insert(0, log_entry)
    data["battle_log"] = data["battle_log"][:BATTLE_LOG_MAX]
    _save(data)
    return {"ok": True, "hp": c["hp"]}

# ── 週重置 ──────────────────────────────────────────────────────────────────
def weekly_reset() -> dict:
    data = _load()
    for uid, c in data["castles"].items():
        c["hp"]              = MAX_HP
        c["wall_level"]      = 0
        c["has_shield"]      = False
        c["points"]          = 0
        c["army"]            = {"soldier": 0, "elite": 0, "catapult": 0}
        c["ban_until"]       = None
        c["fallen_at"]       = None
        c["recruit_cd"]      = {"soldier": None, "elite": None, "catapult": None}
        c["shield_cd_until"] = None
    data["battle_log"] = []
    _save(data)
    return {"ok": True, "reset_at": _tw_now()}

# ── 查詢 ────────────────────────────────────────────────────────────────────
def get_all_castles() -> list:
    data = _load()
    result = []
    for uid, c in data["castles"].items():
        banned, ban_msg = _is_banned(c)
        defense = c.get("wall_level", 0) * WALL_DEF
        cd = _cd_info(c)
        result.append({
            **c, "defense": defense,
            "is_banned": banned, "ban_msg": ban_msg,
            **cd,
        })
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
    cd = _cd_info(c)
    return {**c, "defense": defense, "is_banned": banned, "ban_msg": ban_msg, **cd}
