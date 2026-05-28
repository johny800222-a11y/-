"""
Faker 爬蟲與「台彩獲利最高組合」分析模組
══════════════════════════════════════════════════════════════
核心理論：
  台彩獲利 = 銷售金額 - 獎金支出
  當開出的號碼「沒人買」→ 台彩幾乎不用賠獎金 → 獲利最高
  → 所以 Faker 要找：「會開出但玩家不選的號碼」

分析來源（三層信號）：
  1. 台彩官方 API（最可靠）
     - 用四獎人數/三獎人數反推「每顆號碼的玩家購買熱度」
     - 開獎頻率 × 反向熱度 = Faker 分數

  2. 預測網站爬蟲（補充信號）
     - 爬取公開的 539 預測/分析網站推薦號碼
     - 這些是「聰明玩家愛選的號碼」→ 反向選擇

  3. 號碼組合冷門度
     - 分析哪些數字組合從未一起出現 → 台彩必賺
     - 目標：找到高開出率 × 低玩家選擇率的最佳組合
"""

import json
import ssl
import time
import re
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, Counter

import pytz

_TW       = pytz.timezone("Asia/Taipei")
_DATA_DIR = Path("/data") if Path("/data").exists() else Path(__file__).parent
CROWD_FILE = _DATA_DIR / "faker_crowd_data.json"  # 爬蟲結果快取

NUM_RANGE = list(range(1, 40))
HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str, timeout: int = 10, encoding: str = "utf-8") -> str | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            raw = r.read()
            return raw.decode(encoding, errors="ignore")
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# 信號1：台彩官方 API — 玩家購買熱度
# 邏輯：四獎人數越多 → 這顆號碼被更多玩家買 → 熱門 → 應該避開
# ══════════════════════════════════════════════════════════════

def fetch_player_popularity(months_back: int = 6) -> dict[int, float]:
    """
    從台彩 API 抓取各期中獎人數，
    計算每顆號碼的「玩家購買熱度指數」。

    方法：
      四獎人數（中1顆）代表有多少人買了這顆號碼
      三獎人數（中2顆）代表有多少人買了這個號碼組合
      標準化後加權 → 玩家購買熱度（越高越應該避開）
    """
    now = datetime.now(_TW)
    all_records = []

    for i in range(months_back):
        d = now.replace(day=1) - timedelta(days=i * 28)
        month = d.strftime("%Y-%m")
        url = (
            f"https://api.taiwanlottery.com/TLCAPIWeB/Lottery/Daily539Result"
            f"?month={month}&pageNum=1&pageSize=60"
        )
        text = _get(url, encoding="utf-8")
        if not text:
            continue
        try:
            data = json.loads(text)
            recs = data.get("content", {}).get("daily539Res", [])
            all_records.extend(recs)
        except Exception:
            continue
        time.sleep(0.3)

    if not all_records:
        return {n: 1.0 for n in NUM_RANGE}

    # 累計每顆號碼出現時的四獎 & 三獎人數（標準化為每百萬元銷售）
    fourth_ratios = defaultdict(list)
    third_ratios  = defaultdict(list)

    for rec in all_records:
        nums  = rec.get("drawNumberSize", [])
        sell  = max(rec.get("sellAmount", 1), 1)
        fourth = rec.get("d539FourthAssign", {}).get("winnerCount", 0)
        third  = rec.get("d539ThirdAssign",  {}).get("winnerCount", 0)

        fourth_r = fourth / sell * 1_000_000
        third_r  = third  / sell * 1_000_000

        for n in nums:
            fourth_ratios[int(n)].append(fourth_r)
            third_ratios[int(n)].append(third_r)

    popularity = {}
    for n in NUM_RANGE:
        f_avg = sum(fourth_ratios[n]) / len(fourth_ratios[n]) if fourth_ratios[n] else 0.0
        t_avg = sum(third_ratios[n])  / len(third_ratios[n])  if third_ratios[n]  else 0.0
        # 四獎權重 60%，三獎 40%（三獎更能反映「組合」熱門）
        popularity[n] = 0.6 * f_avg + 0.4 * t_avg

    # 正規化到 0~1
    mn, mx = min(popularity.values()), max(popularity.values())
    if mx > mn:
        popularity = {n: (v - mn) / (mx - mn) for n, v in popularity.items()}

    return popularity


# ══════════════════════════════════════════════════════════════
# 信號2：公開預測網站爬蟲 — 聰明玩家愛選的號碼
# 爬取分析站的推薦號碼 → 這些是「熱門推薦」→ Faker 要反向
# ══════════════════════════════════════════════════════════════

def crawl_prediction_sites() -> dict[int, int]:
    """
    爬取多個 539 分析/預測網站，統計每顆號碼被推薦的次數。
    被推薦次數越多 = 玩家越愛選 = Faker 越應該避開。
    回傳 {num: 被推薦次數}
    """
    mention_count = defaultdict(int)
    num_pattern   = re.compile(r'\b([1-9]|[1-3][0-9])\b')

    # ── 目標1：pilio 近期統計（直接從開獎資料衍生熱號）
    text = _get("https://www.pilio.idv.tw/lto539/list539BIG.asp?indexpage=1&orderby=new", encoding="big5")
    if text:
        # 抓最近5期開獎號碼作為「近期熱號」信號
        rows = re.findall(r'(\d{2}),\s*(\d{2}),\s*(\d{2}),\s*(\d{2}),\s*(\d{2})', text)
        for row in rows[:5]:
            for n in row:
                n = int(n)
                if 1 <= n <= 39:
                    mention_count[n] += 1   # 近期開出 = 玩家追熱門號

    # ── 目標2：Google 搜尋「今彩539熱號推薦」摘要（不跟進，只抓摘要文字）
    for query in ["今彩539熱門號碼", "539推薦號碼", "今彩539必中"]:
        encoded = urllib.request.quote(query)
        url = f"https://www.google.com/search?q={encoded}&num=5&hl=zh-TW"
        text = _get(url, timeout=12)
        if text:
            nums = num_pattern.findall(text[:8000])
            for n_str in nums:
                n = int(n_str)
                if 1 <= n <= 39:
                    mention_count[n] += 1
        time.sleep(1.0)

    # ── 目標3：PTT Lottery 版 標題/內文掃描
    text = _get("https://www.ptt.cc/bbs/Lottery/index.html")
    if text:
        nums = num_pattern.findall(text[:5000])
        for n_str in nums:
            n = int(n_str)
            if 1 <= n <= 39:
                mention_count[n] += 1

    # 補 0（確保所有號碼都有值）
    for n in NUM_RANGE:
        if n not in mention_count:
            mention_count[n] = 0

    return dict(mention_count)


# ══════════════════════════════════════════════════════════════
# 信號3：歷史開獎頻率 — 真實開出機率
# ══════════════════════════════════════════════════════════════

def calc_draw_frequency() -> dict[int, float]:
    """
    從本地開獎歷史計算每顆號碼的真實開出頻率（正規化 0~1）。
    """
    from pathlib import Path
    import csv

    csv_path = _DATA_DIR / "539_history.csv"
    if not csv_path.exists():
        return {n: 1.0 for n in NUM_RANGE}

    freq = defaultdict(int)
    total_rows = 0
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rows += 1
            for col in ["n1", "n2", "n3", "n4", "n5"]:
                try:
                    freq[int(row[col])] += 1
                except (KeyError, ValueError):
                    pass

    if not total_rows:
        return {n: 1.0 for n in NUM_RANGE}

    raw = {n: freq.get(n, 0) / total_rows for n in NUM_RANGE}
    mn, mx = min(raw.values()), max(raw.values())
    return {n: (v - mn) / (mx - mn) if mx > mn else 0.5 for n, v in raw.items()}


# ══════════════════════════════════════════════════════════════
# 整合計算：台彩獲利最高組合
# ══════════════════════════════════════════════════════════════

def compute_faker_scores(
    popularity: dict[int, float],
    mention: dict[int, int],
    frequency: dict[int, float],
) -> dict[int, float]:
    """
    Faker 最終評分公式：

    score(n) =
      0.40 × draw_frequency(n)                   ← 開出機率高（這顆號會開）
      0.35 × (1 - player_popularity(n))          ← 玩家不愛買（台彩少賠錢）
      0.25 × (1 - mention_norm(n))               ← 不在推薦熱榜（避開聰明玩家）

    三個信號相乘 → 取分數最高5顆
    = 「會開出但最沒人買」的組合 = 台彩獲利最高
    """
    # 正規化 mention_count
    m_vals = list(mention.values())
    m_min, m_max = min(m_vals), max(m_vals)
    if m_max > m_min:
        mention_norm = {n: (mention[n] - m_min) / (m_max - m_min) for n in NUM_RANGE}
    else:
        mention_norm = {n: 0.5 for n in NUM_RANGE}

    scores = {}
    for n in NUM_RANGE:
        scores[n] = (
            0.40 * frequency.get(n, 0.5) +
            0.35 * (1.0 - popularity.get(n, 0.5)) +
            0.25 * (1.0 - mention_norm.get(n, 0.5))
        )
    return scores


def save_crowd_data(data: dict):
    CROWD_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_crowd_data() -> dict | None:
    if CROWD_FILE.exists():
        try:
            d = json.loads(CROWD_FILE.read_text())
            # 快取超過 24 小時就重抓
            updated = d.get("updated_at", "")
            if updated:
                dt = datetime.fromisoformat(updated)
                if (datetime.now() - dt).total_seconds() < 86400:
                    return d
        except Exception:
            pass
    return None


def update_crowd_data() -> dict:
    """
    完整更新流程：
    1. 抓台彩 API 玩家熱度
    2. 爬預測網站推薦
    3. 計算歷史開出頻率
    4. 整合 Faker 評分
    5. 快取結果
    """
    popularity = fetch_player_popularity(months_back=6)
    mention    = crawl_prediction_sites()
    frequency  = calc_draw_frequency()
    scores     = compute_faker_scores(popularity, mention, frequency)

    # 排名
    ranked = sorted(NUM_RANGE, key=lambda n: -scores[n])

    # 最佳5顆（台彩獲利最高組合）
    top5 = sorted(ranked[:5])

    # 玩家最愛 vs 最冷門
    most_popular  = sorted(NUM_RANGE, key=lambda n: -popularity.get(n, 0))[:10]
    least_popular = sorted(NUM_RANGE, key=lambda n:  popularity.get(n, 0))[:10]
    most_mentioned = sorted(NUM_RANGE, key=lambda n: -mention.get(n, 0))[:10]

    result = {
        "updated_at":     datetime.now().isoformat(),
        "top5":           top5,
        "scores":         {str(n): round(scores[n], 4) for n in NUM_RANGE},
        "popularity":     {str(n): round(popularity.get(n, 0), 4) for n in NUM_RANGE},
        "mention":        {str(n): mention.get(n, 0) for n in NUM_RANGE},
        "frequency":      {str(n): round(frequency.get(n, 0), 4) for n in NUM_RANGE},
        "most_popular":   most_popular,
        "least_popular":  least_popular,
        "most_mentioned": most_mentioned,
        "analysis": {
            "top10_faker_score": ranked[:10],
            "bottom10_faker":    ranked[-10:],
        },
    }
    save_crowd_data(result)
    return result


def get_faker_recommendation() -> dict:
    """
    主入口：取得 Faker 最新推薦（優先用快取，過期就重算）
    """
    cached = load_crowd_data()
    if cached:
        return cached
    return update_crowd_data()


def generate_report_text(data: dict) -> str:
    """產生分析報告文字（供 TG 推播）"""
    top5 = data.get("top5", [])
    mp   = data.get("most_popular", [])[:8]
    lp   = data.get("least_popular", [])[:8]
    mm   = data.get("most_mentioned", [])[:8]

    lines = [
        "🃏 Faker 策略分析 — 台彩獲利最高組合",
        "═" * 38,
        "",
        f"🎯 今日推薦（台彩獲利最高）：",
        f"   {'  '.join(f'{n:02d}' for n in top5)}",
        "",
        "📊 三信號分析",
        f"   🔥 玩家最愛選（應避開）：{mp}",
        f"   ❄️  玩家最冷門：{lp}",
        f"   📣 網路推薦熱榜（應避開）：{mm}",
        "",
        "💡 策略說明",
        "   開出機率高 × 玩家不買 × 不在推薦榜",
        "   = 台彩幾乎不用賠錢的最優組合",
        f"📅 更新時間：{data.get('updated_at', 'N/A')[:16]}",
        "═" * 38,
    ]
    return "\n".join(lines)
