"""
學習資料備份與還原模組
────────────────────────────────────────────
每日 01:00 自動將所有學習資料打包成 JSON，
以文件形式上傳到 Telegram，並記錄 file_id。

還原時直接從 Telegram 下載最新備份，
一鍵覆寫本地 /data 檔案。
"""

import json
import os
import ssl
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

import pytz

_TW = pytz.timezone("Asia/Taipei")
_DATA_DIR = Path("/data") if Path("/data").exists() else Path(__file__).parent
BACKUP_META_FILE = _DATA_DIR / "backup_meta.json"

# 備份的目標檔案（所有演算法迭代學習資料）
BACKUP_FILES = {
    # 539 迭代學習權重（號碼命中率加成/懲罰，每期對獎後更新）
    "lottery_learn":  _DATA_DIR / "learn_state.json",
    # Bingo 迭代學習權重（熱度+冷號+時段偏好+關聯數對）
    "bingo_learn":    _DATA_DIR / "bingo_learn.json",
    # Bingo 投注快照記錄（對獎依據）
    "bingo_tracker":  _DATA_DIR / "bingo_invest.json",
    # 539 中獎人數分析資料
    "prize_tracker":  _DATA_DIR / "prize_tracker.json",
    # Faker 策略追蹤（推薦記錄 + 命中歷史）
    "faker_tracker":  _DATA_DIR / "faker_tracker.json",
}


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _tg_credentials():
    token   = os.environ.get("TG_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    return token, chat_id


def load_meta() -> dict:
    if BACKUP_META_FILE.exists():
        try:
            return json.loads(BACKUP_META_FILE.read_text())
        except Exception:
            pass
    return {"backups": [], "last_backup": None}


def save_meta(meta: dict):
    BACKUP_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2))


# ── 備份 ──────────────────────────────────────────────────────────────────────

def create_backup() -> dict:
    """打包所有學習資料為一個 JSON bundle"""
    bundle = {
        "version":    2,
        "created_at": datetime.now(_TW).strftime("%Y-%m-%d %H:%M"),
        "files":      {}
    }
    for key, path in BACKUP_FILES.items():
        if path.exists():
            try:
                bundle["files"][key] = json.loads(path.read_text())
            except Exception:
                bundle["files"][key] = None
    return bundle


def send_backup_to_telegram() -> dict:
    """將備份 JSON 上傳到 Telegram，回傳 file_id"""
    token, chat_id = _tg_credentials()
    if not token or not chat_id:
        return {"ok": False, "error": "TG 未設定"}

    bundle = create_backup()
    now_str = datetime.now(_TW).strftime("%Y-%m-%d_%H%M")
    filename = f"lottery_backup_{now_str}.json"
    content  = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")

    # 用 multipart/form-data 上傳文件
    boundary = "----BackupBoundary"
    body  = f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/json\r\n\r\n"
    body += content
    body += f"\r\n--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="caption"\r\n\r\n'
    body += f"\U0001f4be 學習資料備份 {now_str}".encode("utf-8")
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as r:
            result = json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not result.get("ok"):
        return {"ok": False, "error": result.get("description", "unknown")}

    file_id = result["result"]["document"]["file_id"]

    # 記錄 meta
    meta = load_meta()
    meta["backups"].append({
        "date":    now_str,
        "file_id": file_id,
        "files":   list(bundle["files"].keys()),
    })
    meta["backups"] = meta["backups"][-30:]  # 保留最近 30 筆
    meta["last_backup"] = now_str
    save_meta(meta)

    return {"ok": True, "file_id": file_id, "filename": filename, "date": now_str}


# ── 還原 ──────────────────────────────────────────────────────────────────────

def restore_from_telegram(file_id: str = None) -> dict:
    """
    從 Telegram 下載最新備份並還原到 /data。
    file_id=None 時自動取最新一筆。
    """
    token, chat_id = _tg_credentials()
    if not token:
        return {"ok": False, "error": "TG_TOKEN 未設定"}

    # 取 file_id
    if file_id is None:
        meta = load_meta()
        if not meta["backups"]:
            return {"ok": False, "error": "無備份記錄"}
        file_id = meta["backups"][-1]["file_id"]

    # 取下載 URL
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as r:
            info = json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not info.get("ok"):
        return {"ok": False, "error": info.get("description")}

    file_path = info["result"]["file_path"]
    dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"

    req2 = urllib.request.Request(dl_url)
    try:
        with urllib.request.urlopen(req2, timeout=30, context=_ssl_ctx()) as r:
            bundle = json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # 還原檔案
    restored = []
    for key, file_data in bundle.get("files", {}).items():
        if file_data is None:
            continue
        path = BACKUP_FILES.get(key)
        if path:
            path.write_text(json.dumps(file_data, ensure_ascii=False, indent=2))
            restored.append(key)

    return {
        "ok":       True,
        "restored": restored,
        "backup_date": bundle.get("created_at"),
    }


def get_status() -> dict:
    meta = load_meta()
    return {
        "last_backup": meta.get("last_backup"),
        "total_backups": len(meta.get("backups", [])),
        "latest_file_id": meta["backups"][-1]["file_id"] if meta.get("backups") else None,
    }
