#!/usr/bin/env python3
"""
fetch_jobs.py — 从多个学术招聘网站抓取职位，写入 Google Sheets（工作 tab）
来源：jobs.ac.uk（按学科 RSS）、Times Higher Education Jobs（全球 RSS + 关键词过滤）、ReliefWeb RSS
用法：
  python fetch_jobs.py           # 增量模式（跳过已见职位）
  python fetch_jobs.py --all     # 全量模式（忽略 seen 记录，写入全部当前职位）
  python fetch_jobs.py --the-only  # 只跑 THE Jobs，快速测试
  python fetch_jobs.py --week    # 限速模式：jobs.ac.uk 每科只取5条，用于本地验证
列：发现日期 | 学科 | 机构 | 职位 | 薪资 | 申请截止日期 | 申请链接 | 来源
"""

import re, sys, json, html, subprocess, os, time, random
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import gspread
from google.oauth2.service_account import Credentials

# ── 配置 ─────────────────────────────────────────────────────────────────
SHEET_ID    = "1MCcEqV2OGkxFofWSRI6BW2OFYG35cNDHC2olbm43NWc"
SHEET_RANGE = "工作"
SGT         = timezone(timedelta(hours=8))
_now        = datetime.now(SGT)
TODAY       = _now.strftime("%Y-%m-%d")
_date_from  = (_now - timedelta(days=7)).strftime("%Y/%m/%d")
_date_to    = _now.strftime("%Y/%m/%d")
DATE_LABEL  = f"{_date_from}-{_date_to}"
SEEN_FILE   = "/tmp/seen_jobs.json"   # Cloud Run 只有 /tmp 可写

RESET_ALL   = "--all"      in sys.argv
THE_ONLY    = "--the-only" in sys.argv
WEEK_MODE   = "--week"     in sys.argv   # 每学科只取5条，加速本地验证

BASE = "https://www.jobs.ac.uk"
RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept":     "application/rss+xml, application/xml, text/xml, */*",
}
_CURL_BASE = [
    "curl", "-sL", "--max-time", "20", "--compressed",
    "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: en-GB,en;q=0.9",
    "-H", "Accept-Encoding: gzip, deflate, br",
    "-H", "Connection: keep-alive",
]

# ── THE Jobs 配置 ─────────────────────────────────────────────────────────
THE_RSS_FEEDS = [
    ("Sociology",       "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=sociology"),
    ("Social Science",  "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=social+science"),
    ("Politics",        "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=political+science"),
    ("Psychology",      "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=psychology"),
    ("Philosophy",      "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=philosophy"),
    ("History",         "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=history"),
    ("Anthropology",    "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=anthropology"),
    ("Media Studies",   "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=media+studies"),
    ("Cultural Studies","https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=cultural+studies"),
    ("Management",      "https://www.timeshighereducation.com/unijobs/jobsrss/?keywords=management"),
]
THE_DAYS = 9   # 过去几天内发布的职位（周跑用 9 天，保留缓冲避免漏抓）

THE_KEYWORD_MAP = [
    ("history of art",          "History of Art"),
    ("art history",             "History of Art"),
    ("human resources",         "Human Resources Management"),
    ("social policy",           "Social Policy"),
    ("social work",             "Social Work"),
    ("social geography",        "Human & Social Geography"),
    ("human geography",         "Human & Social Geography"),
    ("cultural studies",        "Cultural Studies"),
    ("cultural geography",      "Cultural Studies"),
    ("media studies",           "Media & Communications"),
    ("media and communication", "Media & Communications"),
    ("communication studies",   "Media & Communications"),
    ("journalism",              "Media & Communications"),
    ("publishing",              "Media & Communications"),
    ("sociology",               "Sociology"),
    ("anthropolog",             "Anthropology"),
    ("political science",       "Politics & Government"),
    ("politics",                "Politics & Government"),
    ("government",              "Politics & Government"),
    ("international relations", "Politics & Government"),
    ("philosophy",              "Philosophy"),
    ("psychology",              "Psychology"),
    ("history",                 "History"),
    ("management",              "Management"),
    ("business studies",        "Business Studies"),
    ("business school",         "Business Studies"),
    ("criminology",             "Sociology"),
    ("gender studies",          "Sociology"),
    ("social science",          "Other Social Sciences"),
    ("development studies",     "Other Social Sciences"),
    ("public policy",           "Other Social Sciences"),
    ("demography",              "Other Social Sciences"),
]

# ── jobs.ac.uk 学科配置 ────────────────────────────────────────────────────
SUBJECT_FEEDS = [
    ("Sociology",                           "/jobs/sociology/?format=rss"),
    ("Anthropology",                        "/jobs/anthropology/?format=rss"),
    ("Social Policy",                       "/jobs/social-policy/?format=rss"),
    ("Social Work",                         "/jobs/social-work/?format=rss"),
    ("Politics & Government",               "/jobs/politics-and-government/?format=rss"),
    ("Cultural Studies",                    "/jobs/cultural-studies/?format=rss"),
    ("Human & Social Geography",            "/jobs/human-and-social-geography/?format=rss"),
    ("Other Social Sciences",               "/jobs/other-social-sciences/?format=rss"),
    ("Business Studies",                    "/jobs/business-studies/?format=rss"),
    ("Human Resources Management",          "/jobs/human-resources-management/?format=rss"),
    ("Management",                          "/jobs/management/?format=rss"),
    ("Other Business & Management Studies", "/jobs/other-business-and-management-studies/?format=rss"),
    ("History",                             "/jobs/history/?format=rss"),
    ("History of Art",                      "/jobs/history-of-art/?format=rss"),
    ("Philosophy",                          "/jobs/philosophy/?format=rss"),
    ("Psychology",                          "/jobs/psychology/?format=rss"),
    ("Media & Communications",              "/jobs/media-studies/?format=rss"),
    ("Media & Communications",              "/jobs/journalism/?format=rss"),
    ("Media & Communications",              "/jobs/communication-studies/?format=rss"),
    ("Media & Communications",              "/jobs/publishing/?format=rss"),
]

# International_Orgs 放最后（ReliefWeb 使用）
TARGET_SUBJECTS = list(dict.fromkeys(s for s, _ in SUBJECT_FEEDS)) + ["International_Orgs"]

# ── ReliefWeb RSS 配置 ────────────────────────────────────────────────────
RW_RSS_FEEDS = [
    ("Social Sciences", "https://reliefweb.int/jobs/rss.xml?career_category=5&source_active=1"),
    ("Query",           "https://reliefweb.int/jobs/rss.xml?query%5Bvalue%5D=social+science"),
]

# ── 已见职位记录 ──────────────────────────────────────────────────────────
def load_seen():
    if RESET_ALL:
        return set()
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen), f)
    except Exception as e:
        print(f"⚠️  seen_jobs 写入失败（非致命）: {e}")

# ── 工具函数 ──────────────────────────────────────────────────────────────
def _strip_tags(s):
    return re.sub(r'<[^>]+>', '', s).strip()

def _fix_entities(content_bytes):
    """修复 RSS 中不合规的裸 & 实体"""
    return re.sub(
        rb'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)',
        rb'&amp;', content_bytes)

def parse_rss_description(desc_raw):
    """RSS description → (机构名, 薪资)，双重 HTML 编码"""
    text = html.unescape(html.unescape(desc_raw))
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    sal_m = re.search(r'Salary\s*[:\-]?\s*(.+)', text, re.IGNORECASE)
    if sal_m:
        salary      = sal_m.group(1).strip()
        institution = text[:sal_m.start()].strip().rstrip('|').strip()
    else:
        salary, institution = "", text.strip()
    return institution, salary

_MONTHS = (r'Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
           r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?')
_DATE_PAT = rf'\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}'

_MONTHS_MAP = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
               "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}

def _parse_go_live(raw):
    """'20th February 2026' → '2026-02-20'"""
    m = re.match(r'(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})',
                 (raw or "").strip(), re.IGNORECASE)
    if m:
        d, mon, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _MONTHS_MAP.get(mon)
        if mo:
            return f"{y}-{mo:02d}-{d:02d}"
    return ""

def _parse_pubdate(date_str):
    """解析 RSS pubDate（RFC 2822）→ 带时区 datetime；失败返回 None"""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str.strip())
    except Exception:
        return None

# ── HTTP（curl）─────────────────────────────────────────────────────────
def _curl_get(url):
    """curl 抓页面，返回 HTML 字符串；失败返回空串"""
    try:
        result = subprocess.run(_CURL_BASE + [url], capture_output=True, timeout=25)
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _curl_head_location(url):
    """curl HEAD 跟随重定向，返回最终 URL（用于 /click/ 跳转）"""
    try:
        result = subprocess.run(
            ["curl", "-sI", "-L", "--max-time", "10",
             "-H", "User-Agent: Mozilla/5.0 Chrome/120.0.0.0", url],
            capture_output=True, timeout=15)
        text = result.stdout.decode("utf-8", errors="replace")
        locations = re.findall(r'^Location:\s*(\S+)', text, re.IGNORECASE | re.MULTILINE)
        if locations:
            last = locations[-1]
            if last.startswith('http'):
                return last
    except Exception:
        pass
    return url

# ── 职位详情页抓取 ────────────────────────────────────────────────────────
def _parse_job_json(page):
    """从页面提取 var job = {...} JSON（jobs.ac.uk 专用）"""
    m = re.search(r'var\s+job\s*=\s*(\{.*?\});\s*\n', page, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data.get("job", data)
    except Exception:
        return {}

def scrape_detail(url):
    """返回 (closing_date, apply_url, posted_date, inst)
    - jobs.ac.uk : var job JSON → closing / apply / go_live_date；inst=""
    - THE Jobs   : JSON-LD validThrough → closing；applicationUrl → apply；inst=""
    - ReliefWeb  : 详情页提取机构名和截止日期；apply_url 直接用 reliefweb.int 页面
    """
    time.sleep(random.uniform(0.3, 1.2))
    try:
        page = _curl_get(url)
        if not page:
            return "", url, "", ""

        is_the = "timeshighereducation.com" in url
        is_rw  = "reliefweb.int"           in url
        closing, apply_url, posted_date, inst = "", url, "", ""

        # ══ ReliefWeb ══════════════════════════════════════════════════
        if is_rw:
            # 申请链接：直接用 reliefweb.int 页面（含申请信息）
            apply_url = url

            # 机构名：优先从 JSON-LD hiringOrganization
            for blk in re.findall(
                    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    page, re.DOTALL):
                try:
                    d = json.loads(blk.strip())
                    org = d.get('hiringOrganization', {})
                    if isinstance(org, dict) and org.get('name'):
                        inst = org['name'].strip()
                        break
                except Exception:
                    pass

            # 机构名：/organization/ 链接（最可靠）
            if not inst:
                org_m = re.search(
                    r'<a[^>]+href=["\']?/organization/[^"\'>\s]+["\']?[^>]*>([^<]+)</a>',
                    page, re.IGNORECASE)
                if org_m:
                    inst = org_m.group(1).strip()

            # 机构名：Source / Organization 标签
            if not inst:
                src_m = re.search(
                    r'(?:Source|Organization)\s*[:\-]?\s*<[^>]*>([^<]{2,80})</[^>]*>',
                    page, re.IGNORECASE)
                if src_m:
                    inst = _strip_tags(src_m.group(1)).strip()

            # 截止日期：文本模式
            closing_m = re.search(
                r'[Cc]losing\s+[Dd]ate\s*[:\-]?\s*(' + _DATE_PAT + r')', page, re.IGNORECASE)
            if closing_m:
                closing = closing_m.group(1).strip()
            if not closing:
                cd_m = re.search(
                    r'[Cc]losing\s+[Dd]ate.*?(\d{4}-\d{2}-\d{2})', page, re.DOTALL)
                if cd_m:
                    closing = cd_m.group(1)

            return closing, apply_url, posted_date, inst

        # ══ THE Jobs ═══════════════════════════════════════════════════
        if is_the:
            # apply URL：从 script 块取 "applicationUrl"（含 \u002F 编码）
            m = re.search(r'"applicationUrl"\s*:\s*"([^"]+)"', page, re.IGNORECASE)
            if m:
                raw_url = m.group(1)
                try:
                    decoded = json.loads(f'"{raw_url}"')
                except Exception:
                    decoded = raw_url.replace('\\u002F', '/').replace('\\/', '/')
                if decoded.startswith('http') and 'timeshighereducation' not in decoded:
                    apply_url = decoded

            # closing：JSON-LD validThrough（"validThrough": "2026-03-15T..."）
            for blk in re.findall(
                    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    page, re.DOTALL):
                try:
                    d = json.loads(blk.strip())
                    vt = d.get('validThrough', '')
                    if vt:
                        closing = vt[:10]   # YYYY-MM-DD
                        break
                except Exception:
                    pass

            # closing：<dt>Closing date</dt><dd>...</dd> HTML 结构
            if not closing:
                m2 = re.search(
                    r'<dt[^>]*>\s*Closing date\s*</dt>\s*<dd[^>]*>(.*?)</dd>',
                    page, re.IGNORECASE | re.DOTALL)
                if m2:
                    closing = _strip_tags(m2.group(1))

            # closing：文本模式兜底
            if not closing:
                for pat in [
                    r'[Aa]pplication\s+[Dd]eadline\s*[:\-]?\s*(' + _DATE_PAT + r')',
                    r'[Cc]losing\s+[Dd]ate\s*[:\-]?\s*(' + _DATE_PAT + r')',
                    r'[Dd]eadline\s*[:\-]?\s*('         + _DATE_PAT + r')',
                    r'[Aa]pply\s+by\s*[:\-]?\s*('       + _DATE_PAT + r')',
                ]:
                    mc = re.search(pat, page, re.IGNORECASE)
                    if mc:
                        closing = mc.group(1).strip()
                        break

            return closing, apply_url, posted_date, inst   # inst="" for THE Jobs

        # ══ jobs.ac.uk ═════════════════════════════════════════════════
        job_data = _parse_job_json(page)

        # 截止日期：优先 JSON（closing_date 已是 "29th March 2026" 格式）
        if job_data:
            for key in ("closing_date", "expiring_date", "date_closing", "date_expire"):
                val = job_data.get(key)
                if val:
                    if isinstance(val, (int, float)):
                        closing = datetime.utcfromtimestamp(val).strftime("%-d %B %Y")
                    else:
                        closing = str(val).strip()
                    break

        # 截止日期：HTML dt/dd 备用
        if not closing:
            m = re.search(
                r'<dt[^>]*>\s*Closing\s+[Dd]ate\s*</dt>\s*<dd[^>]*>(.*?)</dd>',
                page, re.IGNORECASE | re.DOTALL)
            if m:
                closing = _strip_tags(m.group(1))
        if not closing:
            m = re.search(rf'Closing\s+Date\s*[:\-]\s*({_DATE_PAT})', page, re.IGNORECASE)
            if m:
                closing = m.group(1).strip()
        if not closing:
            m = re.search(rf'closing.{{0,300}}?({_DATE_PAT})', page, re.IGNORECASE | re.DOTALL)
            if m:
                closing = m.group(1).strip()
        if not closing:
            m = re.search(rf'Expir(?:es|y|ation)\s*[:\-]?\s*({_DATE_PAT})', page, re.IGNORECASE)
            if m:
                closing = m.group(1).strip()

        # 发布日期：go_live_date（"20th February 2026"）→ YYYY-MM-DD
        if job_data:
            gl = job_data.get("go_live_date", "")
            if gl:
                posted_date = _parse_go_live(str(gl))
            if not posted_date:
                dp = job_data.get("date_publish")
                if isinstance(dp, (int, float)) and dp:
                    posted_date = datetime.utcfromtimestamp(dp).strftime("%Y-%m-%d")

        # 申请链接：JSON apply_url → 外链 → /click/ 跳转 → /apply/ 路径
        if job_data:
            val = job_data.get("apply_url")
            if val and str(val).startswith("http") and "jobs.ac.uk" not in str(val):
                apply_url = str(val)

        if apply_url == url:
            for m2 in re.finditer(
                    r'<a[^>]+href=["\']?(https?://[^"\'>\s]+)["\']?[^>]*>(.*?)</a>',
                    page, re.IGNORECASE | re.DOTALL):
                href      = m2.group(1)
                link_text = _strip_tags(m2.group(2))
                if 'jobs.ac.uk' in href:
                    continue
                if re.search(r'\bapply\b', link_text + ' ' + href, re.IGNORECASE):
                    apply_url = href
                    break

        # /click/ 跟随重定向
        if apply_url == url:
            m3 = re.search(
                r'href=["\']?(https?://(?:www\.)?jobs\.ac\.uk/job/[^"\'>\s]+/click/[^"\'>\s]*)',
                page, re.IGNORECASE)
            if m3:
                click_url = m3.group(1)
                final = _curl_head_location(click_url)
                apply_url = final if (final and 'jobs.ac.uk' not in final) else click_url

        if apply_url == url:
            m4 = re.search(r'href=["\']?(/job/[^"\'>\s]+/apply/?[^"\'>\s]*)', page, re.IGNORECASE)
            if m4:
                apply_url = BASE + m4.group(1)

        return closing, apply_url, posted_date, ""   # inst="" for jobs.ac.uk

    except Exception:
        return "", url, "", ""


# ── jobs.ac.uk RSS 抓取 ───────────────────────────────────────────────────
def fetch_rss(subject, path):
    url = BASE + path
    try:
        req = urllib.request.Request(url, headers=RSS_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read()
        content = _fix_entities(content)
        root  = ET.fromstring(content)
        items = root.findall(".//item")
        print(f"  [{subject}] {len(items)} 条")
        return items
    except Exception as e:
        print(f"  [{subject}] 失败: {e}")
        return []


# ── THE Jobs 抓取 ────────────────────────────────────────────────────────
def _the_classify(title, desc):
    """关键词映射学科；无匹配返回 None"""
    text = (title + " " + desc).lower()
    for keyword, subject in THE_KEYWORD_MAP:
        if keyword in text:
            return subject
    return None

def fetch_the_jobs(seen):
    """从 THE Jobs 多个关键词 RSS 抓取职位，用 pubDate 过滤最近 THE_DAYS 天"""
    from datetime import timezone as _tz
    cutoff     = datetime.now(_tz.utc) - timedelta(days=THE_DAYS)
    seen_links = set()
    all_links  = set()
    new_jobs   = []

    for feed_label, url in THE_RSS_FEEDS:
        try:
            result  = subprocess.run(_CURL_BASE + [url], capture_output=True, timeout=25)
            content = _fix_entities(result.stdout)
            root    = ET.fromstring(content)
            items   = root.findall(".//item")
        except Exception as e:
            print(f"  [THE/{feed_label}] 失败: {e}")
            continue

        new_in_feed = 0
        for item in items:
            raw_link = (item.findtext("link") or "").strip()
            if not raw_link:
                continue
            link = re.sub(r'\?.*$', '', raw_link).rstrip('/') + '/'
            all_links.add(link)

            if link in seen_links or link in seen:
                continue
            seen_links.add(link)

            pub_dt = _parse_pubdate(item.findtext("pubDate", ""))
            if not RESET_ALL:
                if pub_dt and pub_dt < cutoff:
                    continue

            pub_date_str = pub_dt.astimezone(SGT).strftime("%Y-%m-%d") if pub_dt else TODAY

            title_raw = (item.findtext("title") or "").strip()
            desc_raw  = html.unescape(item.findtext("description") or "")
            desc_text = re.sub(r'<[^>]+>', ' ', desc_raw)
            desc_text = re.sub(r'\s+', ' ', desc_text).strip()

            subject = _the_classify(title_raw, desc_text)
            if not subject:
                continue   # 不匹配学科体系，跳过

            if ':' in title_raw:
                institution, job_title = title_raw.split(':', 1)
                institution, job_title = institution.strip(), job_title.strip()
            else:
                institution, job_title = "", title_raw

            sal_m = re.search(
                r'(\$[\d,.]+|£[\d,.]+|€[\d,.]+|[\d,.]+\s*(?:USD|GBP|EUR|AUD|CAD)'
                r'|Competitive|Not\s+Specified)',
                desc_text[:300], re.IGNORECASE)
            salary = sal_m.group(1).strip() if sal_m else ""

            new_jobs.append({
                "source":  "THE Jobs",
                "subject": subject,
                "date":    pub_date_str,
                "inst":    institution,
                "title":   job_title,
                "salary":  salary,
                "link":    link,
                "closing": "",
                "apply":   link,
            })
            new_in_feed += 1

        print(f"  [THE/{feed_label}] {len(items)} 条RSS → {new_in_feed} 条新")

    print(f"  [THE Jobs] 合计 {len(new_jobs)} 条新职位（{THE_DAYS}天内，已去重）")
    return new_jobs, all_links


# ── ReliefWeb RSS 抓取 ────────────────────────────────────────────────────
def fetch_reliefweb_rss(seen, all_links):
    """从 ReliefWeb RSS 抓取国际机构职位；apply URL 待 enrich_with_details 补充"""
    results   = []
    seen_here = seen | all_links

    for label, url in RW_RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers=RSS_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                content = r.read()
            content = _fix_entities(content)
            root  = ET.fromstring(content)
            items = root.findall(".//item")
            added = 0
            for item in items:
                link = (item.findtext("link") or "").strip()
                if not link or link in seen_here:
                    continue
                seen_here.add(link)

                pub_raw  = item.findtext("pubDate", "")
                pub_dt   = _parse_pubdate(pub_raw)
                job_date = pub_dt.astimezone(SGT).strftime("%Y-%m-%d") if pub_dt else TODAY

                title_raw = (item.findtext("title") or "").strip()
                desc_raw  = html.unescape(item.findtext("description") or "")
                desc_text = _strip_tags(desc_raw)

                # 机构名：ReliefWeb 标题常见格式
                #   "Job Title | Institution"  或  "Job Title - Institution"
                inst  = ""
                title = title_raw
                if " | " in title_raw:
                    parts = title_raw.split(" | ", 1)
                    title, inst = parts[0].strip(), parts[1].strip()
                elif " - " in title_raw:
                    parts = title_raw.rsplit(" - ", 1)
                    title, inst = parts[0].strip(), parts[1].strip()

                # 截止日期：先从 RSS description 尝试
                closing = ""
                cd_m = re.search(
                    r'[Cc]losing\s+[Dd]ate\s*[:\-]?\s*(' + _DATE_PAT + r')',
                    desc_text, re.IGNORECASE)
                if cd_m:
                    closing = cd_m.group(1).strip()

                results.append({
                    "source":  "ReliefWeb",
                    "date":    job_date,
                    "inst":    inst,
                    "title":   title,
                    "salary":  "",      # 国际机构职位薪资不标准，留空
                    "link":    link,    # reliefweb.int 页面，enrich 时会替换为原始链接
                    "closing": closing,
                    "apply":   link,    # 同上，待 enrich 替换
                })
                added += 1

            print(f"  [ReliefWeb/{label}] {len(items)} 条RSS → {added} 条新")
        except Exception as e:
            print(f"  [ReliefWeb/{label}] 失败: {e}")

    return results


# ── 主抓取流程 ────────────────────────────────────────────────────────────
def fetch_all(seen):
    jobs_by_subject = {s: [] for s in TARGET_SUBJECTS}
    all_links = set()

    # 1. jobs.ac.uk
    print("\n--- jobs.ac.uk ---")
    if THE_ONLY:
        print("  (跳过，--the-only 模式)")
    else:
        for subject, path in SUBJECT_FEEDS:
            items = fetch_rss(subject, path)
            # --week 模式：每学科只取前5条，加速本地验证
            if WEEK_MODE:
                items = items[:2]
            for item in items:
                link = (item.findtext("link") or "").strip()
                if not link:
                    continue
                all_links.add(link)
                if link in seen:
                    continue
                title    = (item.findtext("title") or "").strip()
                desc_raw = (item.findtext("description") or "").strip()
                institution, salary = parse_rss_description(desc_raw)
                jobs_by_subject[subject].append({
                    "source":  "jobs.ac.uk",
                    "date":    TODAY,
                    "inst":    institution,
                    "title":   title,
                    "salary":  salary,
                    "link":    link,
                    "closing": "",
                    "apply":   link,
                })

    # 2. THE Jobs
    print("\n--- THE Jobs ---")
    the_jobs, the_links = fetch_the_jobs(seen)
    all_links |= the_links
    for j in the_jobs:
        jobs_by_subject[j["subject"]].append(j)

    # 3. ReliefWeb RSS
    print("\n--- ReliefWeb ---")
    rw_jobs = fetch_reliefweb_rss(seen, all_links)
    for j in rw_jobs:
        jobs_by_subject["International_Orgs"].append(j)
        all_links.add(j["link"])
    if rw_jobs:
        print(f"  [ReliefWeb] 合计 {len(rw_jobs)} 条")

    return jobs_by_subject, all_links


# ── 补充详情（并发）─────────────────────────────────────────────────────
def enrich_with_details(jobs_by_subject):
    """并发抓取详情页，补充截止日期、申请链接、发布日期（jobs.ac.uk）、机构名（ReliefWeb）
    scrape_detail 返回 (closing, apply_url, posted_date, inst)
    - jobs.ac.uk : JSON → closing / apply / go_live_date；inst 忽略
    - THE Jobs   : JSON-LD validThrough / applicationUrl；inst 忽略
    - ReliefWeb  : 机构名从详情页提取；apply_url = reliefweb.int 页面
    """
    all_jobs = [j for subj in TARGET_SUBJECTS for j in jobs_by_subject[subj]
                if j["source"] in ("jobs.ac.uk", "THE Jobs", "ReliefWeb")]
    total = len(all_jobs)
    if total == 0:
        return

    print(f"\n抓取 {total} 个职位详情页（并发 5 线程，含随机延迟）...")
    done = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_map = {ex.submit(scrape_detail, j["link"]): j for j in all_jobs}
        for f in as_completed(f_map):
            j = f_map[f]
            closing, apply_url, posted_date, inst = f.result()
            if closing:
                j["closing"] = closing
            j["apply"] = apply_url
            if posted_date:          # jobs.ac.uk 真实发布日期
                j["date"] = posted_date
            if inst and j["source"] == "ReliefWeb":   # ReliefWeb 机构名
                j["inst"] = inst
            done += 1
            if done % 20 == 0 or done == total:
                print(f"  {done}/{total} 完成")


# ── 写入 Google Sheets ────────────────────────────────────────────────────
def write_to_sheets(jobs_by_subject):
    rows = []
    for subj in TARGET_SUBJECTS:
        display_subj = "" if subj == "International_Orgs" else subj
        for j in jobs_by_subject[subj]:
            rows.append([
                "'" + j["date"],   # 加 ' 防止 Sheets 把日期解析成其他格式
                display_subj,
                j["inst"],
                j["title"],
                j["salary"],
                j["closing"],
                j["apply"],
                j.get("source", ""),
            ])

    if not rows:
        print("没有新职位")
        return False

    try:
        import base64
        cred_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
        if cred_json:
            try:
                sa_info = json.loads(base64.b64decode(cred_json))
            except Exception:
                sa_info = json.loads(cred_json)
            creds = Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/spreadsheets",
                        "https://www.googleapis.com/auth/drive"])
        else:
            import google.auth
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/spreadsheets",
                        "https://www.googleapis.com/auth/drive"])
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_RANGE)
        # 时间戳行（置顶）+ 数据 + 空行分隔
        ts            = datetime.now(SGT).strftime("%Y/%m/%d, %H:%M") + "完成更新"
        timestamp_row = [[ts] + [""] * (len(rows[0]) - 1)]
        separator     = [[""] * len(rows[0])]
        ws.insert_rows(timestamp_row + rows + separator, row=2,
                       value_input_option="USER_ENTERED")
        print(f"✓ 成功写入 {len(rows)} 条（已置顶）")
        return True
    except Exception as e:
        print(f"Sheets 写入异常: {e}")
        return False


# ── 主函数 ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = "全量模式（--all）" if RESET_ALL else ("限速模式（--week）" if WEEK_MODE else "增量模式")
    print(f"=== 抓取学术职位 [jobs.ac.uk + THE Jobs + ReliefWeb] [{mode}] ===")
    print(f"📅 抓取范围: {DATE_LABEL}")

    seen = load_seen()
    print(f"已记录 {len(seen)} 条历史职位")

    jobs, all_links = fetch_all(seen)
    total_new = sum(len(v) for v in jobs.values())

    print(f"\n发现 {total_new} 条新职位")
    for subj in TARGET_SUBJECTS:
        if jobs[subj]:
            print(f"  {subj}: {len(jobs[subj])}")

    if total_new:
        enrich_with_details(jobs)
        ok = write_to_sheets(jobs)
        if ok:
            save_seen(seen | all_links)
            print(f"已更新记录（共 {len(seen | all_links)} 条）")
    else:
        save_seen(seen | all_links)
