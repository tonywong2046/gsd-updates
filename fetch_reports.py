#!/usr/bin/env python3
"""
Think Tank Report Fetcher — RSS Edition
每天抓取主要智库最新报告 → 写入 Google Sheets「智库报告」标签
"""
import json, os, re, time, base64, functools
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

# ── Gemini 动态模型选择 ───────────────────────────────────────────────────────
GEMINI_PREFERRED = [
    "gemini-2.5-flash",       # 首选：最新最优 flash
    "gemini-2.0-flash",       # 备选：上一代，极稳定
    "gemini-2.0-flash-lite",  # 再备：更便宜
    "gemini-1.5-flash",       # 兜底：老但极可靠
    "gemini-1.5-flash-8b",    # 最终兜底：最便宜
]
_EXCLUDE_KEYWORDS = ("pro", "preview", "exp", "thinking")

def _model_version_key(name):
    m = re.search(r'gemini-(\d+)[.\-](\d+)', name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

@functools.lru_cache(maxsize=8)
def _list_gemini_models(api_key):
    """列出指定 API key 可用的 Gemini 模型（纯 REST，不依赖 SDK，结果缓存）"""
    try:
        from urllib.request import urlopen as _urlopen
        import json as _json
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=200"
        with _urlopen(url, timeout=10) as r:
            data = _json.loads(r.read())
        return frozenset(
            m["name"].removeprefix("models/")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        )
    except Exception as e:
        print(f"  ⚠️ 无法列出 Gemini 模型: {e}")
        return frozenset()

def get_best_gemini_model(api_key):
    """按优先级选择最佳可用 flash 模型，排除 pro/preview/exp/thinking"""
    available = _list_gemini_models(api_key)
    if not available:
        return "gemini-2.0-flash"  # 列表失败时的默认值
    for model in GEMINI_PREFERRED:
        if model in available:
            return model
    # 所有优先模型均不可用：自动寻找版本最高的 flash 模型
    candidates = [
        m for m in available
        if "flash" in m and not any(kw in m for kw in _EXCLUDE_KEYWORDS)
    ]
    if candidates:
        chosen = max(candidates, key=_model_version_key)
        print(f"  📌 自动降级至: {chosen}")
        return chosen
    return "gemini-1.5-flash"

# ── Think Tank RSS Feeds ──────────────────────────────────────────────────────
# (机构名, 分类标签, RSS URL)
# 已移除 KFF 和 Urban Institute
THINK_TANKS = [
    ("Pew Research Center",          "社会调研", "https://www.pewresearch.org/feed/"),
    ("CEPR",                         "经济政策", "https://cepr.org/rss.xml"),
    ("Our World in Data",            "全球数据", "https://ourworldindata.org/atom.xml"),
    ("Our World in Data (Insights)", "全球数据", "https://ourworldindata.org/atom-data-insights.xml"),
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
    """安全提取 XML 元素文本"""
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
    """过滤附录等正式报告之外的页面"""
    t = title.lower().strip()
    if t in _SKIP_TITLES:
        return True
    if any(t.startswith(kw) for kw in ("appendix", "errata:", "correction:")):
        return True
    return False

def get_atom_link(item):
    """从 Atom entry 提取链接"""
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
    prompt = f"""你是一位社会科学领域的编辑，负责为社会学公众号筛选智库报告。
请对以下标题完成：
1. 判断相关性（relevant true/false）
2. 若相关，用一句中文简介（35字以内）；不相关 score 留空。
具体规则参考社会学、宏观政策。

列表：
{titles_list}

请严格按 JSON 返回：
[
  {{"index": 1, "relevant": true,  "score": "简介文本"}},
  ...
]"""

    def parse_scores(content):
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
        start, end = content.find("["), content.rfind("]") + 1
        return json.loads(content[start:end])

    def apply_scores(scores):
        score_map    = {s["index"]: s.get("score", "暂无简介") for s in scores}
        relevant_set = {s["index"] for s in scores if s.get("relevant", True)}
        for i, a in enumerate(articles):
            a["intro"]    = score_map.get(i + 1, "暂无简介")
            a["relevant"] = (i + 1) in relevant_set

    # 1. Groq（默认，最稳定）
    if GROQ_API_KEY:
        try:
            payload = json.dumps({
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000,
            }).encode()
            req = Request("https://api.groq.com/openai/v1/chat/completions", data=payload,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json", "User-Agent": "curl/7.88.1"})
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            apply_scores(parse_scores(result["choices"][0]["message"]["content"].strip()))
            print("  ✅ 简介生成完成（Groq）")
            return _filter_relevant(articles)
        except Exception as e:
            print(f"  ⚠️  Groq: {e}，尝试 Gemini...")

    # 2. Gemini（备用）
    def call_gemini(api_key):
        model = get_best_gemini_model(api_key)
        print(f"  🤖 使用模型: {model}")
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 2000}
        }).encode()
        req = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
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
                return _filter_relevant(articles)
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

    # 3. OpenRouter
    if OPENROUTER_API_KEY:
        for attempt in range(3):
            try:
                payload = json.dumps({
                    "model": "meta-llama/llama-3.3-70b-instruct:free",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1000,
                }).encode()
                req = Request("https://openrouter.ai/api/v1/chat/completions", data=payload,
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                             "Content-Type": "application/json", "HTTP-Referer": "https://openclaw.ai"})
                with urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                apply_scores(parse_scores(result["choices"][0]["message"]["content"].strip()))
                print("  ✅ 简介生成完成（OpenRouter）")
                return _filter_relevant(articles)
            except Exception as e:
                if "429" in str(e):
                    time.sleep((attempt + 1) * 15)
                else:
                    print(f"  ⚠️  OpenRouter: {e}"); break

    print("  ⚠️  所有模型失败，使用默认值")
    for a in articles:
        a["intro"] = a.get("intro", "暂无简介")
        a["relevant"] = a.get("relevant", True)
    return articles

def _filter_relevant(articles):
    kept = [a for a in articles if a.get("relevant", True)]
    print(f"  🔍 保留 {len(kept)}/{len(articles)} 篇报告")
    return kept

# ── 写入 Google Sheets ────────────────────────────────────────────────────────
def write_to_sheets(articles):
    if not articles: return
    rows = []
    for a in sorted(articles, key=lambda x: x["category"]):
        rows.append(["'" + a["date"], a["category"], a["source"],
                     a["title"], a["intro"], a["link"]])

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        if sa_json:
            # 本地/GitHub Actions：使用 JSON key（Base64 或原始 JSON）
            sa_info = json.loads(base64.b64decode(sa_json))
            creds = Credentials.from_service_account_info(
                sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            # GCP Cloud Run：使用 Application Default Credentials
            import google.auth
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/spreadsheets"])

        gc = gspread.authorize(creds)
        ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
        # 时间戳行 + 数据 + 空行分隔（置顶）
        ts = datetime.now(SGT).strftime("%Y/%m/%d, %H:%M") + "完成更新"
        timestamp_row = [[ts] + [""] * (len(rows[0]) - 1)]
        separator = [[""] * len(rows[0])]
        ws.insert_rows(timestamp_row + rows + separator, row=2, value_input_option="USER_ENTERED")
        print(f"✅ 成功写入 {len(articles)} 篇报告（已置顶）")
    except Exception as e:
        print(f"❌ gspread 写入失败: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🔍 抓取范围: {DATE_FROM} 至 {DATE_TO}")
    all_articles = []
    for name, category, url in THINK_TANKS:
        all_articles.extend(fetch_think_tank(name, category, url))
        time.sleep(0.5)

    if not all_articles:
        print("没有新报告，退出。"); return

    print("🤖 正在生成简介...")
    all_articles = summarize_reports(all_articles)
    
    print("📊 写入 Google Sheets...")
    write_to_sheets(all_articles)

if __name__ == "__main__":
    main()
