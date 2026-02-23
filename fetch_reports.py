#!/usr/bin/env python3
"""
Think Tank Report Fetcher — RSS Edition
每天抓取主要智库最新报告 → 写入 Google Sheets「智库报告」标签
"""
import json, os, re, time, base64
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

# ── Config ────────────────────────────────────────────────────────────────────
SGT         = timezone(timedelta(hours=8))  # 新加坡时间 (SGT)
_now        = datetime.now(SGT)
# 正常运行抓昨天；测试/补抓时可设 LOOKBACK_DAYS=7 等
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))
DATE_FROM   = (_now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
# LOOKBACK_DAYS=1 时只抓昨天；>1 时包含今天（方便测试验证）
DATE_TO     = _now.strftime("%Y-%m-%d") if LOOKBACK_DAYS > 1 else (_now - timedelta(days=1)).strftime("%Y-%m-%d")

SHEET_ID  = "1MCcEqV2OGkxFofWSRI6BW2OFYG35cNDHC2olbm43NWc"
SHEET_TAB = "报告"

GEMINI_KEYS = [k for k in [
    os.environ.get("GEMINI_API_KEY", ""),
    os.environ.get("GEMINI_API_KEY_2", ""),
    os.environ.get("GEMINI_API_KEY_3", ""),
] if k]
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# ── Think Tank RSS Feeds ──────────────────────────────────────────────────────
# (机构名, 分类标签, RSS URL)
THINK_TANKS = [
    ("Pew Research Center",          "社会调研", "https://www.pewresearch.org/feed/"),
    ("KFF",                          "医疗政策", "https://kff.org/feed/"),
    ("Urban Institute",              "社会政策", "https://www.urban.org/research/rss.xml"),
    ("CEPR",                         "经济政策", "https://cepr.org/rss.xml"),
    ("Our World in Data",            "全球数据", "https://ourworldindata.org/atom.xml"),
    ("Council on Foreign Relations", "国际关系", "https://feeds.cfr.org/cfr/publications"),
    ("UN News",                      "国际事务", "https://www.un.org/en/rss.xml"),
    ("Aspen Institute",              "政策科技", "https://www.aspeninstitute.org/feed/"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "KHTML, like Gecko Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

NS_ATOM = "{http://www.w3.org/2005/Atom}"
NS_DC   = "{http://purl.org/dc/elements/1.1/}"

# ── Helpers ───────────────────────────────────────────────────────────────────
def norm_date(date_str):
    """解析各种日期格式 → YYYY-MM-DD（新加坡时间）"""
    if not date_str:
        return ""
    import email.utils
    date_str = date_str.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed.astimezone(SGT).strftime("%Y-%m-%d")
    except:
        pass
    try:
        cleaned = date_str.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        return parsed.astimezone(SGT).strftime("%Y-%m-%d")
    except:
        pass
    if len(date_str) >= 10 and date_str[4] == "-" and date_str[7] == "-":
        return date_str[:10]
    return ""

def get_text(el):
    """安全提取 XML 元素文本（处理 CDATA、嵌套标签）"""
    if el is None:
        return ""
    text = (el.text or "").strip()
    if not text:
        text = "".join(el.itertext()).strip()
    return re.sub(r'<[^>]+>', '', text).strip()

_SKIP_TITLES = [
    "acknowledgments", "acknowledgements", "methodology", "appendix",
    "errata", "correction", "about this report", "about this survey",
    "about pew research", "topline questionnaire", "survey questions",
    "codebook", "about the data", "note on",
]

def is_supplementary(title):
    """过滤附录、方法论、致谢等附属页面，只保留正式报告"""
    t = title.lower().strip()
    # 完整匹配（标题就是这个词）
    if t in _SKIP_TITLES:
        return True
    # 前缀匹配（如 "Appendix A: ..."、"Appendix E: Detailed tables"）
    if any(t.startswith(kw) for kw in ("appendix", "errata:", "correction:")):
        return True
    return False

def get_atom_link(item):
    """从 Atom entry 提取 alternate 链接"""
    for link_el in item.findall(f"{NS_ATOM}link"):
        rel  = link_el.get("rel", "alternate")
        href = link_el.get("href", "")
        if rel in ("alternate", "") and href:
            return href
    link_el = item.find(f"{NS_ATOM}link")
    return (link_el.text or "").strip() if link_el is not None else ""

# ── RSS 抓取 ──────────────────────────────────────────────────────────────────
def fetch_think_tank(name, category, url):
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
        content = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
        root = ET.fromstring(content.encode("utf-8"))

        # 判断是 Atom 还是 RSS 2.0
        is_atom = (root.tag == f"{NS_ATOM}feed" or
                   root.find(f".//{NS_ATOM}entry") is not None)

        if is_atom:
            items = root.findall(f".//{NS_ATOM}entry")
        else:
            items = root.findall(".//item")

        articles = []
        for item in items:
            if is_atom:
                title_el = item.find(f"{NS_ATOM}title")
                _upd     = item.find(f"{NS_ATOM}updated")
                date_el  = _upd if _upd is not None else item.find(f"{NS_ATOM}published")
                link     = get_atom_link(item)
            else:
                title_el = item.find("title")
                _pub     = item.find("pubDate")
                date_el  = _pub if _pub is not None else item.find(f"{NS_DC}date")
                link_el  = item.find("link")
                link     = get_text(link_el) if link_el is not None else ""

            title    = get_text(title_el)
            pub_date = norm_date(get_text(date_el))

            if not title or not pub_date or pub_date < DATE_FROM or pub_date > DATE_TO:
                continue
            if is_supplementary(title):
                continue

            articles.append({
                "source":   name,
                "category": category,
                "title":    title,
                "date":     pub_date,
                "link":     link,
            })

        print(f"  ✅ {name}: {len(articles)} 篇")
        return articles

    except HTTPError as e:
        print(f"  ⚠️  {name}: HTTP {e.code}")
        return []
    except Exception as e:
        print(f"  ⚠️  {name}: 失败 ({e})")
        return []

# ── LLM 中文简介 ──────────────────────────────────────────────────────────────
def summarize_reports(articles):
    if not articles:
        return articles

    titles_list = "\n".join([
        f"{i+1}. [{a['source']}] {a['title']}" for i, a in enumerate(articles)
    ])
    prompt = f"""你是一位政策研究专家。以下是来自各大智库的最新报告标题，请根据标题逐一用一句中文说明这份报告大概在研究什么。

要求：
- 只根据标题推断，不要编造内容
- 每条简介控制在35字以内
- 语言简洁，直接说明研究主题

报告列表：
{titles_list}

请严格按以下JSON格式返回，不要加任何其他文字：
[
  {{"index": 1, "score": "一句话中文简介"}},
  {{"index": 2, "score": "一句话中文简介"}}
]"""

    def parse_scores(content):
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
        start, end = content.find("["), content.rfind("]") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON array: {content[:80]!r}")
        return json.loads(content[start:end])

    def apply_scores(scores):
        score_map = {s["index"]: s["score"] for s in scores}
        for i, a in enumerate(articles):
            a["intro"] = score_map.get(i + 1, "暂无简介")

    # 1. Gemini
    def call_gemini(api_key):
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 2000, "thinkingConfig": {"thinkingBudget": 0}},
        }).encode()
        req = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}",
            data=payload, headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        parts = result["candidates"][0]["content"]["parts"]
        text = next((p["text"] for p in reversed(parts) if "text" in p), "").strip()
        return parse_scores(text)

    for key_idx, api_key in enumerate(GEMINI_KEYS):
        label = f"Gemini key{key_idx+1}"
        for attempt in range(3):
            try:
                apply_scores(call_gemini(api_key))
                print(f"  ✅ 简介生成完成（{label}）")
                return articles
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    if attempt < 2:
                        time.sleep((attempt + 1) * 10)
                        print(f"  ⏳ {label} 限速，重试中...")
                    else:
                        print(f"  ⏳ {label} 持续限速，换下一个 key")
                else:
                    print(f"  ⚠️  {label}: {e}，换下一个 key")
                    break

    # 2. Groq
    if GROQ_API_KEY:
        try:
            payload = json.dumps({
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1500,
            }).encode()
            req = Request("https://api.groq.com/openai/v1/chat/completions", data=payload,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json", "User-Agent": "curl/7.88.1"})
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            apply_scores(parse_scores(result["choices"][0]["message"]["content"].strip()))
            print("  ✅ 简介生成完成（Groq）")
            return articles
        except Exception as e:
            print(f"  ⚠️  Groq: {e}")

    # 3. OpenRouter
    if OPENROUTER_API_KEY:
        for attempt in range(3):
            try:
                payload = json.dumps({
                    "model": "meta-llama/llama-3.3-70b-instruct:free",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1500,
                }).encode()
                req = Request("https://openrouter.ai/api/v1/chat/completions", data=payload,
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                             "Content-Type": "application/json"})
                with urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                apply_scores(parse_scores(result["choices"][0]["message"]["content"].strip()))
                print("  ✅ 简介生成完成（OpenRouter）")
                return articles
            except Exception as e:
                if "429" in str(e):
                    time.sleep((attempt + 1) * 15)
                else:
                    print(f"  ⚠️  OpenRouter: {e}"); break

    print("  ⚠️  所有模型失败，使用默认简介")
    for a in articles:
        a["intro"] = "暂无简介"
    return articles

# ── 写入 Google Sheets ────────────────────────────────────────────────────────
def write_to_sheets(articles):
    if not articles:
        print("没有新报告。"); return

    # 按分类排序
    rows = []
    for a in sorted(articles, key=lambda x: x["category"]):
        rows.append(["'" + a["date"], a["category"], a["source"],
                     a["title"], a["intro"], a["link"]])

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    if sa_json:
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            sa_info = json.loads(base64.b64decode(sa_json))
            creds = Credentials.from_service_account_info(
                sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            gc = gspread.authorize(creds)
            ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
            ws.append_rows(rows, value_input_option="USER_ENTERED",
                           insert_data_option="INSERT_ROWS")
            print(f"✅ 成功写入 {len(articles)} 篇报告到 Google Sheets（gspread）")
        except Exception as e:
            print(f"❌ gspread 写入失败: {e}")
    else:
        import subprocess
        values_json = json.dumps(rows, ensure_ascii=False)
        cmd = ["gog", "sheets", "append", SHEET_ID, SHEET_TAB,
               "--values-json", values_json, "--insert", "INSERT_ROWS"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ 成功写入 {len(articles)} 篇报告（gog）")
        else:
            print(f"❌ 写入失败: {result.stderr.strip()}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🔍 抓取范围: {DATE_FROM} 至 {DATE_TO}（{LOOKBACK_DAYS}天）")
    print(f"📡 {len(THINK_TANKS)} 个智库 RSS 源\n")

    all_articles = []
    for name, category, url in THINK_TANKS:
        articles = fetch_think_tank(name, category, url)
        all_articles.extend(articles)
        time.sleep(0.5)

    print(f"\n📝 共找到 {len(all_articles)} 篇报告")
    if not all_articles:
        print("没有新报告，退出。"); return

    print("🤖 正在生成中文简介...")
    all_articles = summarize_reports(all_articles)

    print("📊 写入 Google Sheets...")
    write_to_sheets(all_articles)

if __name__ == "__main__":
    main()
