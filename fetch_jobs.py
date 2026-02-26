#!/usr/bin/env python3
"""
fetch_jobs.py — 整合版本：保留原逻辑 + 新增 HigherEdJobs & ASA 爬虫
"""

import re, sys, json, html, os, time, random
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
TODAY       = datetime.now(SGT).strftime("%Y-%m-%d")
SEEN_FILE   = os.path.join(os.path.dirname(__file__), "seen_jobs.json")
RESET_ALL   = "--all" in sys.argv
THE_ONLY    = "--the-only" in sys.argv

BASE    = "https://www.jobs.ac.uk"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
RSS_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, application/xml, text/xml, */*"}

# ── THE Jobs 配置 ───────────────────────────────────────────────────────
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

THE_KEYWORD_MAP = [
    ("history of art", "History of Art"), ("art history", "History of Art"),
    ("human resources", "Human Resources Management"), ("social policy", "Social Policy"),
    ("social work", "Social Work"), ("social geography", "Human & Social Geography"),
    ("human geography", "Human & Social Geography"), ("cultural studies", "Cultural Studies"),
    ("media studies", "Media & Communications"), ("media and communication", "Media & Communications"),
    ("sociology", "Sociology"), ("anthropolog", "Anthropology"),
    ("political science", "Politics & Government"), ("politics", "Politics & Government"),
    ("philosophy", "Philosophy"), ("psychology", "Psychology"),
    ("history", "History"), ("management", "Management"),
    ("criminology", "Sociology"), ("gender studies", "Sociology"),
    ("social science", "Other Social Sciences"), ("demography", "Other Social Sciences")
]

# ── jobs.ac.uk 学科配置 ────────────────────────────────────────────────────
SUBJECT_FEEDS = [
    ("Sociology", "/jobs/sociology/?format=rss"),
    ("Anthropology", "/jobs/anthropology/?format=rss"),
    ("Social Policy", "/jobs/social-policy/?format=rss"),
    ("Politics & Government", "/jobs/politics-and-government/?format=rss"),
    ("Human & Social Geography", "/jobs/human-and-social-geography/?format=rss"),
    ("Other Social Sciences", "/jobs/other-social-sciences/?format=rss"),
    ("Management", "/jobs/management/?format=rss"),
    ("History", "/jobs/history/?format=rss"),
    ("Philosophy", "/jobs/philosophy/?format=rss"),
    ("Psychology", "/jobs/psychology/?format=rss"),
    ("Media & Communications", "/jobs/media-studies/?format=rss"),
]

# ReliefWeb 国际组织 RSS
RELIEFWEB_FEEDS = [
    ("International_Orgs", "https://reliefweb.int/jobs/rss.xml?search=sociology"),
    ("International_Orgs", "https://reliefweb.int/jobs/rss.xml?ty=258")
]

# 新增：HigherEdJobs & ASA 配置
HIGHERED_URLS = [
    ("Sociology", "https://www.higheredjobs.com/faculty/search.cfm?JobCat=93&suggest=2"),
    ("Politics & Government", "https://www.higheredjobs.com/faculty/search.cfm?JobCat=169&suggest=2"),
    ("Other Social Sciences", "https://www.higheredjobs.com/faculty/search.cfm?JobCat=209&suggest=2")
]
ASA_URL = "https://careercenter.asanet.org/jobs/faculty/"

TARGET_SUBJECTS = list(dict.fromkeys([s for s, _ in SUBJECT_FEEDS] + ["International_Orgs"]))

# ── 基础功能 ──────────────────────────────────────────────────────────────
def load_seen():
    if RESET_ALL: return set()
    try:
        with open(SEEN_FILE) as f: return set(json.load(f))
    except: return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f: json.dump(list(seen), f)

def parse_rss_description(desc_raw):
    text = html.unescape(html.unescape(desc_raw))
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    sal_m = re.search(r'Salary\s*[:\-]?\s*(.+)', text, re.IGNORECASE)
    if sal_m:
        salary, inst = sal_m.group(1).strip(), text[:sal_m.start()].strip()
    else:
        salary, inst = "", text.strip()
    return inst, salary

def scrape_detail(url):
    time.sleep(random.uniform(0.5, 1.5))
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as res:
            page = res.read().decode('utf-8', errors='replace')
            closing = ""
            m = re.search(r'Closing Date:\s*</span>\s*([^<]+)', page)
            if not m: m = re.search(r'Closing\s+Date\s*[:\-]\s*(\d{1,2}\s+\w+\s+\d{4})', page, re.I)
            if m: closing = m.group(1).strip()
            apply_url = url
            if "jobs.ac.uk" in url:
                m_apply = re.search(r'href="([^"]+/apply/[^"]+)"', page)
                if m_apply: apply_url = BASE + m_apply.group(1) if m_apply.group(1).startswith('/') else m_apply.group(1)
            return closing, apply_url
    except:
        return "", url

# ── 新增抓取逻辑：HigherEdJobs & ASA ──────────────────────────────────────
def fetch_higheredjobs(seen):
    results = []
    for subj, url in HIGHERED_URLS:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                content = r.read().decode('utf-8')
                # 正则匹配职位块
                items = re.findall(r'<a href="details\.cfm\?JobCode=(\d+)[^"]*">([^<]+)</a>.*?<span class="inst-name">([^<]+)</span>', content, re.S)
                for code, title, inst in items:
                    link = f"https://www.higheredjobs.com/faculty/details.cfm?JobCode={code}"
                    if link in seen: continue
                    results.append({
                        "subj": subj, "source": "HigherEdJobs", "date": TODAY, "inst": inst.strip(),
                        "title": title.strip(), "salary": "See Link", "link": link, "closing": "Check Link", "apply": link
                    })
        except: continue
    return results

def fetch_asa(seen):
    results = []
    try:
        req = urllib.request.Request(ASA_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read().decode('utf-8')
            # 匹配 ASA 职位链接和标题
            items = re.findall(r'class="job-title">\s*<a href="([^"]+)">([^<]+)</a>.*?class="job-location">([^<]+)</span>', content, re.S)
            for path, title, inst in items:
                link = "https://careercenter.asanet.org" + path if path.startswith('/') else path
                if link in seen: continue
                results.append({
                    "subj": "Sociology", "source": "ASA", "date": TODAY, "inst": inst.strip(),
                    "title": title.strip(), "salary": "US Based", "link": link, "closing": "N/A", "apply": link
                })
    except: pass
    return results

# ── 抓取逻辑 ──────────────────────────────────────────────────────────────
def fetch_all(seen):
    jobs_by_subject = {s: [] for s in TARGET_SUBJECTS}
    # 确保新增的学科键存在
    for s in ["Sociology", "Politics & Government", "Other Social Sciences"]:
        if s not in jobs_by_subject: jobs_by_subject[s] = []
        
    all_links = set()

    # 1. jobs.ac.uk
    if not THE_ONLY:
        for subj, path in SUBJECT_FEEDS:
            try:
                req = urllib.request.Request(BASE + path, headers=RSS_HEADERS)
                with urllib.request.urlopen(req, timeout=20) as r:
                    root = ET.fromstring(r.read())
                    for item in root.findall(".//item"):
                        link = item.findtext("link").strip()
                        if link in seen: continue
                        inst, sal = parse_rss_description(item.findtext("description", ""))
                        jobs_by_subject[subj].append({
                            "source": "jobs.ac.uk", "date": TODAY, "inst": inst,
                            "title": item.findtext("title").strip(), "salary": sal,
                            "link": link, "closing": "", "apply": link
                        })
                        all_links.add(link)
            except: continue

    # 2. THE Jobs
    for label, url in THE_RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                root = ET.fromstring(r.read())
                for item in root.findall(".//item"):
                    link = item.findtext("link").split('?')[0].strip()
                    if link in seen or link in all_links: continue
                    title = item.findtext("title")
                    subj = "Other Social Sciences"
                    for kw, target_s in THE_KEYWORD_MAP:
                        if kw in title.lower():
                            subj = target_s; break
                    inst = title.split(' at ')[-1] if ' at ' in title else "THE"
                    jobs_by_subject[subj].append({
                        "source": "THE Jobs", "date": TODAY, "inst": inst,
                        "title": title.split(' at ')[0], "salary": "See Link",
                        "link": link, "closing": "Check Link", "apply": link
                    })
                    all_links.add(link)
        except: continue

    # 3. ReliefWeb
    for subj, url in RELIEFWEB_FEEDS:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS)) as r:
                root = ET.fromstring(r.read())
                for item in root.findall(".//item"):
                    link = item.findtext("link")
                    if link in seen or link in all_links: continue
                    title_full = item.findtext("title")
                    title = title_full.split(" | ")[0]
                    inst = title_full.split(" | ")[1] if " | " in title_full else "ReliefWeb"
                    jobs_by_subject[subj].append({
                        "source": "ReliefWeb", "date": TODAY, "inst": inst,
                        "title": title, "salary": "International",
                        "link": link, "closing": "N/A", "apply": link
                    })
                    all_links.add(link)
        except: continue

    # 4. HigherEdJobs (新增)
    he_jobs = fetch_higheredjobs(seen | all_links)
    for j in he_jobs:
        jobs_by_subject[j["subj"]].append(j)
        all_links.add(j["link"])

    # 5. ASA (新增)
    asa_jobs = fetch_asa(seen | all_links)
    for j in asa_jobs:
        jobs_by_subject[j["subj"]].append(j)
        all_links.add(j["link"])

    return jobs_by_subject, all_links

# ── 写入 Google Sheets ───────────────────────────────────────────────────
def write_to_sheets(jobs_dict):
    rows = []
    for subj, items in jobs_dict.items():
        for j in items:
            rows.append([j["date"], subj, j["inst"], j["title"], j["salary"], j["closing"], j["apply"], j["source"]])
    
    if not rows: return False

    try:
        import base64
        cred_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
        if not cred_json:
            print("错误: 未找到 GOOGLE_SERVICE_ACCOUNT 环境变量")
            return False
        # 兼容 Base64 编码或原始 JSON 字符串
        try:
            sa_info = json.loads(base64.b64decode(cred_json))
        except Exception:
            sa_info = json.loads(cred_json)
        creds = Credentials.from_service_account_info(sa_info,
                scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'])
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_RANGE)
        # 新数据置顶：在第 2 行（标题行之后）插入，确保最新数据在最上方
        ws.insert_rows(rows, row=2, value_input_option="USER_ENTERED")
        print(f"✓ 成功写入 {len(rows)} 条（已置顶）")
        return True
    except Exception as e:
        print(f"Sheets 写入异常: {e}")
        return False

# ── 执行 ──────────────────────────────────────────────────────────────────
def main():
    seen = load_seen()
    jobs, all_links = fetch_all(seen)
    
    # 补充详情 (仅针对 jobs.ac.uk)
    ac_jobs = [j for items in jobs.values() for j in items if j["source"] == "jobs.ac.uk"]
    if ac_jobs:
        with ThreadPoolExecutor(max_workers=5) as ex:
            f_map = {ex.submit(scrape_detail, j["link"]): j for j in ac_jobs}
            for f in as_completed(f_map):
                j = f_map[f]
                j["closing"], j["apply"] = f.result()

    if write_to_sheets(jobs):
        save_seen(seen | all_links)

if __name__ == "__main__":
    main()
