#!/usr/bin/env python3
"""
fetch_jobs.py — 从多个学术招聘网站抓取职位，写入 Google Sheets（工作 tab）
来源：jobs.ac.uk（按学科 RSS）、Times Higher Education Jobs（全球 RSS + 关键词过滤）
策略：RSS 无发布日期，用「已见职位」记录增量更新；并发抓详情页获取截止日期和申请链接
用法：python fetch_jobs.py [--all]   # --all 忽略 seen 记录，写入全部当前职位
列：发现日期 | 来源 | 学科 | 机构 | 职位 | 薪资 | 申请截止日期 | 申请链接
"""

import re, sys, json, html, subprocess, os, time, random
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request

# ── 配置 ─────────────────────────────────────────────────────────────────
SHEET_ID    = "1MCcEqV2OGkxFofWSRI6BW2OFYG35cNDHC2olbm43NWc"
SHEET_RANGE = "工作"
SGT         = timezone(timedelta(hours=8))
TODAY       = datetime.now(SGT).strftime("%Y-%m-%d")
SEEN_FILE   = os.path.join(os.path.dirname(__file__), "seen_jobs.json")
RESET_ALL   = "--all" in sys.argv
THE_ONLY    = "--the-only" in sys.argv   # 只跑 THE Jobs，用于测试

BASE    = "https://www.jobs.ac.uk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
}
RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ── THE Jobs (Times Higher Education) ────────────────────────────────────────
# 关键词 RSS（每条返回最新 20 个匹配职位，带 pubDate，可按日期过滤）
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

# 关键词 → 映射到我们的学科分类（按优先顺序匹配）
THE_KEYWORD_MAP = [
    ("history of art",            "History of Art"),
    ("art history",               "History of Art"),
    ("human resources",           "Human Resources Management"),
    ("social policy",             "Social Policy"),
    ("social work",               "Social Work"),
    ("social geography",          "Human & Social Geography"),
    ("human geography",           "Human & Social Geography"),
    ("cultural studies",          "Cultural Studies"),
    ("cultural geography",        "Cultural Studies"),
    ("media studies",             "Media & Communications"),
    ("media and communication",   "Media & Communications"),
    ("communication studies",     "Media & Communications"),
    ("journalism",                "Media & Communications"),
    ("publishing",                "Media & Communications"),
    ("sociology",                 "Sociology"),
    ("anthropolog",               "Anthropology"),
    ("political science",         "Politics & Government"),
    ("politics",                  "Politics & Government"),
    ("government",                "Politics & Government"),
    ("international relations",   "Politics & Government"),
    ("philosophy",                "Philosophy"),
    ("psychology",                "Psychology"),
    ("history",                   "History"),
    ("management",                "Management"),
    ("business studies",          "Business Studies"),
    ("business school",           "Business Studies"),
    ("criminology",               "Sociology"),
    ("gender studies",            "Sociology"),
    ("social science",            "Other Social Sciences"),
    ("development studies",       "Other Social Sciences"),
    ("public policy",             "Other Social Sciences"),
    ("demography",                "Other Social Sciences"),
]

# ── 科目 → RSS URL ────────────────────────────────────────────────────────
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

TARGET_SUBJECTS = list(dict.fromkeys(s for s, _ in SUBJECT_FEEDS))


# ── 已见职位记录 ──────────────────────────────────────────────────────────

def load_seen() -> set:
    if RESET_ALL:
        return set()
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ── 解析 RSS description ──────────────────────────────────────────────────

def parse_rss_description(desc_raw: str):
    """RSS description: '机构<br />Salary: £xxx'（双重 HTML 编码）"""
    text = html.unescape(html.unescape(desc_raw))
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    sal_m = re.search(r'Salary\s*[:\-]?\s*(.+)', text, re.IGNORECASE)
    if sal_m:
        salary      = sal_m.group(1).strip()
        institution = text[:sal_m.start()].strip().rstrip('|').strip()
    else:
        salary      = ""
        institution = text.strip()

    return institution, salary


# ── 抓取职位详情页（截止日期 + 官方申请链接）────────────────────────────

# 月份名用于日期解析
_MONTHS = (r'Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
           r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?')
_DATE_PAT = rf'\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}'


def _strip_tags(s: str) -> str:
    return re.sub(r'<[^>]+>', '', s).strip()


_CURL_BASE = [
    "curl", "-sL",
    "--max-time", "20",
    "--compressed",
    "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "-H", "Accept-Language: en-GB,en;q=0.9",
    "-H", "Accept-Encoding: gzip, deflate, br",
    "-H", "Connection: keep-alive",
]

def _curl_get(url: str) -> str:
    """用 curl 抓页面，返回 HTML 字符串；失败返回空串"""
    try:
        result = subprocess.run(
            _CURL_BASE + [url],
            capture_output=True, timeout=25
        )
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _curl_head_location(url: str) -> str:
    """用 curl HEAD 跟随重定向，返回最终 URL"""
    try:
        result = subprocess.run(
            ["curl", "-sI", "-L", "--max-time", "10",
             "-H", "User-Agent: Mozilla/5.0 Chrome/120.0.0.0",
             url],
            capture_output=True, timeout=15
        )
        # 找最后一个 Location: 头
        text = result.stdout.decode("utf-8", errors="replace")
        locations = re.findall(r'^Location:\s*(\S+)', text, re.IGNORECASE | re.MULTILINE)
        if locations:
            last = locations[-1]
            if last.startswith('http'):
                return last
    except Exception:
        pass
    return url


def _parse_job_json(page: str) -> dict:
    """从页面提取 var job = {...} 的 JSON，返回解析后的 job dict"""
    m = re.search(r'var\s+job\s*=\s*(\{.*?\});\s*\n', page, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data.get("job", data)
    except Exception:
        return {}


def _format_date(raw: str) -> str:
    """把 '2026-03-15' 或 '2026-03-15T00:00:00' 转成 '15 March 2026'"""
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', raw or "")
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12:
            return f"{d} {MONTHS[mo-1]} {y}"
    return raw.strip() if raw else ""


def _parse_go_live(raw: str) -> str:
    """把 '20th February 2026' 转成 '2026-02-20'（YYYY-MM-DD）；失败返回空串"""
    MONTHS = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    m = re.match(r'(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})', (raw or "").strip(), re.IGNORECASE)
    if m:
        d, mon, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = MONTHS.get(mon)
        if mo:
            return f"{y}-{mo:02d}-{d:02d}"
    return ""


def scrape_detail(jobs_ac_url: str):
    """返回 (closing_date, apply_url, posted_date)
    posted_date: jobs.ac.uk 用 go_live_date（YYYY-MM-DD），THE Jobs 为空（RSS 已有 pubDate）
    """
    # 随机小延迟，降低被封概率
    time.sleep(random.uniform(0.3, 1.2))
    try:
        page = _curl_get(jobs_ac_url)
        if not page:
            return "", jobs_ac_url, ""

        # ── 截止日期：优先从 JSON 拿（仅 jobs.ac.uk；THE Jobs 用 HTML）────
        closing = ""
        is_the_jobs = "timeshighereducation.com" in jobs_ac_url
        job_data = {} if is_the_jobs else _parse_job_json(page)
        if job_data:
            # jobs.ac.uk 实际字段名（调试确认）
            for key in ("closing_date", "expiring_date", "date_closing", "date_expire"):
                val = job_data.get(key)
                if val:
                    # closing_date 已是 "29th March 2026" 格式，date_closing 是 Unix 时间戳
                    if isinstance(val, (int, float)):
                        from datetime import datetime
                        closing = datetime.utcfromtimestamp(val).strftime("%-d %B %Y")
                    else:
                        closing = str(val).strip()
                    break

        # 备用：从 HTML 结构匹配
        if not closing:
            m = re.search(
                r'<dt[^>]*>\s*Closing\s+[Dd]ate\s*</dt>\s*<dd[^>]*>(.*?)</dd>',
                page, re.IGNORECASE | re.DOTALL)
            if m:
                closing = _strip_tags(m.group(1))

        if not closing:
            m = re.search(
                rf'Closing\s+Date\s*[:\-]\s*({_DATE_PAT})',
                page, re.IGNORECASE)
            if m:
                closing = m.group(1).strip()

        if not closing:
            m = re.search(
                rf'closing.{{0,300}}?({_DATE_PAT})',
                page, re.IGNORECASE | re.DOTALL)
            if m:
                closing = m.group(1).strip()

        if not closing:
            m = re.search(
                rf'Expir(?:es|y|ation)\s*[:\-]?\s*({_DATE_PAT})',
                page, re.IGNORECASE)
            if m:
                closing = m.group(1).strip()

        # ── 发布日期（jobs.ac.uk 专用）────────────────────────────────────
        posted_date = ""
        if job_data and "timeshighereducation.com" not in jobs_ac_url:
            # 优先用 go_live_date（"20th February 2026" 格式）
            gl = job_data.get("go_live_date", "")
            if gl:
                posted_date = _parse_go_live(str(gl))
            # 备用：date_publish Unix 时间戳
            if not posted_date:
                dp = job_data.get("date_publish")
                if isinstance(dp, (int, float)) and dp:
                    posted_date = datetime.utcfromtimestamp(dp).strftime("%Y-%m-%d")

        # ── 官方申请链接 ──────────────────────────────────────────────────
        apply_url = jobs_ac_url   # 最终 fallback

        if is_the_jobs:
            # THE Jobs：从 <script> 块里找 "applicationUrl"（含 \u002F 编码）
            m = re.search(r'"applicationUrl"\s*:\s*"([^"]+)"', page, re.IGNORECASE)
            if m:
                raw_url = m.group(1)
                # 解码 \u002F → /  以及 \/ → /
                try:
                    decoded = json.loads(f'"{raw_url}"')
                except Exception:
                    decoded = raw_url.replace('\\u002F', '/').replace('\\/', '/')
                if decoded.startswith('http') and 'timeshighereducation' not in decoded:
                    apply_url = decoded

        else:
            # jobs.ac.uk：从 JSON 拿 apply_url
            if job_data:
                val = job_data.get("apply_url")
                if val and str(val).startswith("http") and "jobs.ac.uk" not in str(val):
                    apply_url = str(val)

            # 找页面上直接指向外部机构的链接
            if apply_url == jobs_ac_url:
                for m in re.finditer(
                        r'<a[^>]+href=["\']?(https?://[^"\'>\s]+)["\']?[^>]*>(.*?)</a>',
                        page, re.IGNORECASE | re.DOTALL):
                    href      = m.group(1)
                    link_text = _strip_tags(m.group(2))
                    if 'jobs.ac.uk' in href:
                        continue
                    if re.search(r'\bapply\b', link_text + ' ' + href, re.IGNORECASE):
                        apply_url = href
                        break

            # 找 /click/ 跳转链接并跟随重定向
            if apply_url == jobs_ac_url:
                m = re.search(
                    r'href=["\']?(https?://(?:www\.)?jobs\.ac\.uk/job/[^"\'>\s]+/click/[^"\'>\s]*)',
                    page, re.IGNORECASE)
                if m:
                    click_url = m.group(1)
                    final = _curl_head_location(click_url)
                    if final and 'jobs.ac.uk' not in final:
                        apply_url = final
                    else:
                        apply_url = click_url

            # 找内部 /apply 路径
            if apply_url == jobs_ac_url:
                m = re.search(r'href=["\']?(/job/[^"\'>\s]+/apply/?[^"\'>\s]*)', page, re.IGNORECASE)
                if m:
                    apply_url = BASE + m.group(1)

        return closing, apply_url, posted_date

    except Exception:
        return "", jobs_ac_url, ""


# ── RSS 抓取 ──────────────────────────────────────────────────────────────

def fetch_rss(subject: str, path: str):
    url = BASE + path
    try:
        req = urllib.request.Request(url, headers=RSS_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read()
        content = re.sub(
            rb'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)',
            rb'&amp;', content)
        root  = ET.fromstring(content)
        items = root.findall(".//item")
        print(f"  [{subject}] {len(items)} items")
        return items
    except Exception as e:
        print(f"  [{subject}] 失败: {e}")
        return []


# ── THE Jobs 抓取 ─────────────────────────────────────────────────────────

def _the_classify(title: str, desc: str) -> str | None:
    """用关键词把 THE Jobs 职位映射到我们的学科分类，无匹配返回 None"""
    text = (title + " " + desc).lower()
    for keyword, subject in THE_KEYWORD_MAP:
        if keyword in text:
            return subject
    return None


def _parse_pubdate(date_str: str):
    """解析 RSS pubDate（RFC 2822），返回带时区的 datetime，失败返回 None"""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str.strip())
    except Exception:
        return None


def fetch_the_jobs(seen: set):
    """从 THE Jobs 多个关键词 RSS 抓取职位，用 pubDate 过滤最近 THE_DAYS 天"""
    from datetime import timezone as _tz
    cutoff     = datetime.now(_tz.utc) - timedelta(days=THE_DAYS)
    seen_links = set()   # 跨 feed 内部去重
    all_links  = set()
    new_jobs   = []

    for feed_label, url in THE_RSS_FEEDS:
        try:
            result = subprocess.run(_CURL_BASE + [url], capture_output=True, timeout=25)
            content = result.stdout
            content = re.sub(
                rb'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)',
                rb'&amp;', content)
            root  = ET.fromstring(content)
            items = root.findall(".//item")
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

            # 跨 feed 去重 + 跳过历史已见
            if link in seen_links or link in seen:
                continue
            seen_links.add(link)

            # pubDate 过滤：只要最近 THE_DAYS 天内发布的
            pub_dt = _parse_pubdate(item.findtext("pubDate", ""))
            if not RESET_ALL:
                if pub_dt and pub_dt < cutoff:
                    continue

            # 把 pubDate 转成 YYYY-MM-DD（新加坡时间，UTC+8）
            pub_date_str = TODAY
            if pub_dt:
                pub_date_str = pub_dt.astimezone(SGT).strftime("%Y-%m-%d")

            title_raw = (item.findtext("title") or "").strip()
            desc_raw  = html.unescape(item.findtext("description") or "")
            desc_text = re.sub(r'<[^>]+>', ' ', desc_raw)
            desc_text = re.sub(r'\s+', ' ', desc_text).strip()

            subject = _the_classify(title_raw, desc_text)
            if not subject:
                continue  # 关键词不匹配我们的学科体系，跳过

            # "INSTITUTION: Job Title" 格式
            if ':' in title_raw:
                institution, job_title = title_raw.split(':', 1)
                institution = institution.strip()
                job_title   = job_title.strip()
            else:
                institution, job_title = "", title_raw

            # 薪资从 description 开头提取
            sal_m = re.search(
                r'(\$[\d,.]+|£[\d,.]+|€[\d,.]+|[\d,.]+\s*(?:USD|GBP|EUR|AUD|CAD)|Competitive|Not\s+Specified)',
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


# ── 主流程 ───────────────────────────────────────────────────────────────

def fetch_all(seen: set):
    # jobs_by_subject: {subject: [job_dict, ...]}
    # job_dict 含 source 字段区分来源
    jobs_by_subject = {s: [] for s in TARGET_SUBJECTS}
    all_links = set()

    # ── jobs.ac.uk ────────────────────────────────────────────────────
    print("\n--- jobs.ac.uk ---")
    if THE_ONLY:
        print("  (跳过，--the-only 模式)")
    for subject, path in ([] if THE_ONLY else SUBJECT_FEEDS):
        items = fetch_rss(subject, path)
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

    # ── THE Jobs ──────────────────────────────────────────────────────
    print("\n--- THE Jobs ---")
    the_jobs, the_links = fetch_the_jobs(seen)
    all_links |= the_links
    for j in the_jobs:
        jobs_by_subject[j["subject"]].append(j)

    return jobs_by_subject, all_links


def enrich_with_details(jobs_by_subject: dict):
    """并发抓取每个职位的详情页，补充截止日期；THE Jobs 申请链接不变"""
    all_jobs = [(subj, j) for subj in TARGET_SUBJECTS for j in jobs_by_subject[subj]]
    total = len(all_jobs)
    if total == 0:
        return

    print(f"\n抓取 {total} 个职位详情页（并发 5 线程，含随机延迟）...")
    done = 0

    with ThreadPoolExecutor(max_workers=5) as ex:
        future_to_job = {
            ex.submit(scrape_detail, j["link"]): j
            for _, j in all_jobs
        }
        for future in as_completed(future_to_job):
            j = future_to_job[future]
            closing, apply_url, posted_date = future.result()
            j["closing"] = closing
            j["apply"]   = apply_url
            if posted_date:          # 用真实发布日期替换抓取日
                j["date"] = posted_date
            done += 1
            if done % 20 == 0 or done == total:
                print(f"  {done}/{total} 完成")


def write_to_sheets(jobs_by_subject: dict):
    total = sum(len(v) for v in jobs_by_subject.values())
    if total == 0:
        print("没有新职位")
        return False

    rows = []
    for subject in TARGET_SUBJECTS:
        for j in jobs_by_subject[subject]:
            rows.append([
                "'" + j["date"],        # 发现日期
                subject,                # 学科
                j["inst"],              # 机构
                j["title"],             # 职位
                j["salary"],            # 薪资
                j["closing"],           # 申请截止日期
                j["apply"],             # 链接
                j.get("source", ""),    # 来源
            ])

    values_json = json.dumps(rows, ensure_ascii=False)
    cmd = ["gog", "sheets", "append", SHEET_ID, SHEET_RANGE,
           "--values-json", values_json, "--insert", "INSERT_ROWS"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print(f"✓ 成功写入 {total} 条新职位到「{SHEET_RANGE}」tab")
        return True
    else:
        print(f"✗ Sheets 写入失败:\n{res.stderr}")
        return False


if __name__ == "__main__":
    mode = "全量模式（--all）" if RESET_ALL else "增量模式（只写新职位）"
    print(f"=== 抓取学术职位 [jobs.ac.uk + THE Jobs] [{mode}] ===")

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
            new_seen = seen | all_links
            save_seen(new_seen)
            print(f"已更新记录（共 {len(new_seen)} 条）")
    else:
        save_seen(seen | all_links)
