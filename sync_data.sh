#!/bin/bash
# 從 Railway 同步最新資料到本機 /data 目錄
# 用法：./sync_data.sh

API="https://lottery539-production.up.railway.app/api/data/export"
DATA_DIR="$(dirname "$0")"   # 存在專案目錄（本機沒有 /data）

echo "📥 正在從 Railway 同步最新資料..."

python3 - <<EOF
import urllib.request, json, base64, os

url = "$API"
data_dir = "$DATA_DIR"

try:
    with urllib.request.urlopen(url, timeout=30) as resp:
        result = json.loads(resp.read())
except Exception as e:
    print(f"❌ 連線失敗：{e}")
    exit(1)

if not result.get("ok"):
    print("❌ API 回傳錯誤")
    exit(1)

files = result.get("files", {})
for fname, b64content in files.items():
    fpath = os.path.join(data_dir, fname)
    content = base64.b64decode(b64content)
    with open(fpath, "wb") as f:
        f.write(content)
    size = len(content)
    print(f"  ✅ {fname} ({size:,} bytes)")

print(f"\n✅ 同步完成，共 {len(files)} 個檔案")
EOF
