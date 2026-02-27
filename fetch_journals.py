#!/usr/bin/env python3
"""
Sociology Journal Fetcher — CrossRef API Edition
- 国际期刊：CrossRef API（按 ISSN + 日期查询，无需 RSS URL）
- 过滤书评 → Gemini/Groq 评分 → 写入 Google Sheets
"""

import subprocess, json, os, re, functools
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request

# ── Config ───────────────────────────────────────────────────────────────────
SHEET_ID    = "1MCcEqV2OGkxFofWSRI6BW2OFYG35cNDHC2olbm43NWc"
SHEET_RANGE = "论文"
MAILTO      = "wangsenhu@gmail.com"   # CrossRef polite pool
GEMINI_KEYS = [k for k in [
    os.environ.get("GEMINI_API_KEY", ""),
    os.environ.get("GEMINI_API_KEY_2", ""),
    os.environ.get("GEMINI_API_KEY_3", ""),
] if k]
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
SGT = timezone(timedelta(hours=8))  # 新加坡时间 (SGT)
TARGET_DATE = (datetime.now(SGT) - timedelta(days=1)).strftime("%Y-%m-%d")

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
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=200"
        with urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        return frozenset(
            m["name"].removeprefix("models/")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        )
    except Exception as e:
        print(f"   ⚠️ 无法列出 Gemini 模型: {e}")
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
        print(f"   📌 自动降级至: {chosen}")
        return chosen
    return "gemini-1.5-flash"

# ── 国际期刊（CrossRef，按 ISSN）────────────────────────────────────────────
JOURNALS = [
    # 综合社会学
    ("American Sociological Review",      "综合社会学",    "0003-1224"),
    ("American Journal of Sociology",     "综合社会学",    "0002-9602"),
    ("Annual Review of Sociology",        "综合社会学",    "0360-0572"),
    ("Social Forces",                     "综合社会学",    "0037-7732"),
    ("Sociological Methods & Research",   "综合社会学",    "0049-1241"),
    ("European Sociological Review",      "综合社会学",    "0266-7215"),
    ("British Journal of Sociology",      "综合社会学",    "0007-1315"),
    ("Sociology (BSA)",                   "综合社会学",    "0038-0385"),
    ("Work, Employment and Society",      "综合社会学",    "0950-0170"),
    ("Chinese Sociological Review",       "综合社会学",    "2162-0555"),
    # 移民与族裔
    ("International Migration Review",           "移民与族裔", "0197-9183"),
    ("Journal of Ethnic and Migration Studies",  "移民与族裔", "1369-183X"),
    ("International Migration",                  "移民与族裔", "0020-7985"),
    # 计算社会科学
    ("Journal of Computational Social Science", "计算社会科学", "2432-2717"),
    ("Social Science Computer Review",          "计算社会科学", "0894-4393"),
    ("Nature Human Behaviour",                  "计算社会科学", "2397-3374"),
    # 社会网络
    ("Social Networks",  "社会网络", "0378-8733"),
    ("Network Science",  "社会网络", "2050-1242"),
    # 社会分层与流动
    ("Research in Social Stratification and Mobility", "社会分层与流动", "0276-5624"),
    ("Social Science Research",                         "社会分层与流动", "0049-089X"),
    # 医学社会学
    ("Social Science & Medicine",           "医学社会学", "0277-9536"),
    ("Journal of Health and Social Behavior","医学社会学", "0022-1465"),
    ("Sociology of Health & Illness",       "医学社会学", "0141-9889"),
    # 老年学
    ("Journals of Gerontology Series B", "老年学", "1079-5014"),
    ("The Gerontologist",                "老年学", "0016-9013"),
    ("Journal of Aging and Health",      "老年学", "0898-2643"),
    # 婚姻与家庭
    ("Journal of Marriage and Family", "婚姻与家庭", "0022-2445"),
    ("Journal of Family Issues",       "婚姻与家庭", "0192-513X"),
    # 人口学
    ("Demography",                      "人口学", "0070-3370"),
    ("Population and Development Review","人口学", "0098-7921"),
    ("Population Studies",              "人口学", "0032-4728"),
    ("European Journal of Population",  "人口学", "0168-6577"),
    ("Demographic Research",            "人口学", "1435-9871"),
]


# ── 书评过滤 ─────────────────────────────────────────────────────────────────
BOOK_REVIEW_KEYWORDS = [
    "book review", "review of ", "reviews of ",
    "book notice", "book symposium", "review essay", "book forum",
]

def is_book_review(title):
    t = title.lower()
    if any(kw in t for kw in BOOK_REVIEW_KEYWORDS):
        return True
    if re.search(r'\bISBN\b', title, re.IGNORECASE):
        return True
    if re.search(r'\bpp\.', title) and re.search(r'[£$€]\d', title):
        return True
    if re.search(r'\. By [A-Z].+?\.\s+\w+:', title):
        return True
    return False

# ── CrossRef 抓取 ─────────────────────────────────────────────────────────────
def fetch_crossref(journal_name, field, issn):
    import time
    url = (
        f"https://api.crossref.org/works"
        f"?filter=issn:{issn},from-pub-date:{TARGET_DATE},until-pub-date:{TARGET_DATE}"
        f"&rows=50&select=title,author,DOI,URL,published,published-online,type"
        f"&mailto={MAILTO}"
    )
    req = Request(url, headers={"User-Agent": f"SociologyBot/1.0 (mailto:{MAILTO})"})
    data = None
    for attempt in range(4):
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            break
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = (attempt + 1) * 15
                print(f"   ⏳ {journal_name}: 限速，{wait}秒后重试...")
                time.sleep(wait)
            else:
                print(f"   ⚠️  {journal_name}: 失败 ({e})")
                return []
    if data is None:
        return []
    try:
        items = data.get("message", {}).get("items", [])
        articles = []
        for item in items:
            if item.get("type") != "journal-article":
                continue

            title_list = item.get("title", [])
            title = re.sub(r'<[^>]+>', '', title_list[0]).strip() if title_list else ""
            if not title or is_book_review(title):
                continue

            # 日期：优先 published-online
            pub = item.get("published-online") or item.get("published") or {}
            parts = pub.get("date-parts", [[]])[0]
            if len(parts) >= 3:
                article_date = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
            else:
                continue  # 日期不完整跳过

            if article_date != TARGET_DATE:
                continue

            # 作者
            authors = []
            for a in item.get("author", []):
                name = f"{a.get('given','')} {a.get('family','')}".strip()
                if name:
                    authors.append(name)

            doi  = item.get("DOI", "")
            link = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")

            articles.append({
                "journal": journal_name, "field": field,
                "title":   title,
                "authors": ", ".join(authors) or "N/A",
                "date":    article_date,
                "link":    link,
            })

        print(f"   ✅ {journal_name}: {len(articles)} 篇")
        return articles
    except Exception as e:
        print(f"   ⚠️  {journal_name}: 失败 ({e})")
        return []


# ── 评分 ─────────────────────────────────────────────────────────────────────
def score_articles(articles):
    if not articles:
        return articles
    import time

    titles_list = "\n".join([
        f"{i+1}. [{a['journal']}] {a['title']}" for i, a in enumerate(articles)
    ])
    prompt = f"""你是社会学领域的专家教授。请根据以下学术论文的题目，逐一用一句中文简介说明这篇论文大概在研究什么。

要求：
- 只根据题目推断，不要编造内容
- 每条简介控制在30字以内
- 语言简洁，直接说明研究主题

论文列表：
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
            a["score"] = score_map.get(i + 1, "暂无简介")

    # 1. Gemini（动态模型选择）
    def call_gemini(api_key):
        model = get_best_gemini_model(api_key)
        print(f"   🤖 使用模型: {model}")
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 2000},
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
                print(f"   ✅ 评分完成（{label}）")
                return articles
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    if attempt < 2:
                        time.sleep((attempt + 1) * 10)
                        print(f"   ⏳ {label} 限速，重试中...")
                    else:
                        print(f"   ⏳ {label} 持续限速，换下一个 key")
                else:
                    print(f"   ⚠️  {label}: {e}，换下一个 key")
                    break

    # 2. Groq
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
            print("   ✅ 评分完成（Groq）")
            return articles
        except Exception as e:
            print(f"   ⚠️  Groq: {e}")

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
                print("   ✅ 评分完成（OpenRouter）")
                return articles
            except Exception as e:
                if "429" in str(e):
                    time.sleep((attempt + 1) * 15)
                else:
                    print(f"   ⚠️  OpenRouter: {e}"); break

    print("   ⚠️  所有评分模型失败，使用默认评分")
    for a in articles:
        a["score"] = "暂无简介"
    return articles

# ── 写入 Google Sheets ────────────────────────────────────────────────────────
def write_to_sheets(articles):
    if not articles:
        print("没有新文章。"); return

    from collections import defaultdict
    dates_order, by_date = [], defaultdict(list)
    for a in articles:
        if a["date"] not in by_date:
            dates_order.append(a["date"])
        by_date[a["date"]].append(a)

    rows = []
    for i, date in enumerate(sorted(dates_order)):
        day_articles = sorted(by_date[date], key=lambda x: x["field"])
        for a in day_articles:
            rows.append(["'" + a["date"], a["field"], a["journal"],
                         a["authors"], a["title"], a["score"], a["link"]])
        if i < len(dates_order) - 1:
            rows.append(["", "", "", "", "", "", ""])

    # 环境检测：如果有 GOOGLE_SERVICE_ACCOUNT，说明在 GitHub/云端运行
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    try:
        import gspread, base64
        from google.oauth2.service_account import Credentials

        if sa_json:
            # 本地/GitHub Actions：使用 JSON key（Base64 或原始 JSON）
            try:
                sa_info = json.loads(base64.b64decode(sa_json))
            except:
                sa_info = json.loads(sa_json)
            creds = Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            # GCP Cloud Run：使用 Application Default Credentials
            import google.auth
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/spreadsheets"])

        gc = gspread.authorize(creds)
        ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_RANGE)
        # 新数据置顶：空行追加在本批数据后面，视觉上区分每次抓取批次
        separator = [["" ] * len(rows[0])]
        ws.insert_rows(rows + separator, row=2, value_input_option="USER_ENTERED")
        print(f"✅ 成功写入 {len(articles)} 篇文章到 Google Sheets（已置顶）")
    except Exception as e:
        print(f"❌ gspread 写入失败: {e}")

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f"🔍 抓取日期: {TARGET_DATE}")
    print(f"📚 {len(JOURNALS)} 个国际期刊（CrossRef）\n")

    all_articles = []

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_crossref, n, f, i): n for n, f, i in JOURNALS}
        for future in as_completed(futures):
            all_articles.extend(future.result())

    print(f"\n📝 共找到 {len(all_articles)} 篇昨天的文章")
    if not all_articles:
        print("没有新文章，退出。"); return

    print("🤖 正在评分（单次 LLM 调用）...")
    all_articles = score_articles(all_articles)

    print("📊 写入 Google Sheets...")
    write_to_sheets(all_articles)

if __name__ == "__main__":
    main()
