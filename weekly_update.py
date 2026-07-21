#!/usr/bin/env python3
"""每周自动抓取大话西游2维护公告并更新到数据文件"""
import json, re, os, sys, shutil
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# 配置
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'announcements_data.js')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'}


def log(msg):
    print(msg, flush=True)


def load_data():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        js = f.read()
    json_file = os.path.join(os.path.dirname(DATA_FILE), 'announcements_data.json')
    if os.path.exists(json_file):
        with open(json_file, 'r', encoding='utf-8') as f2:
            return json.load(f2)
    m = re.search(r'window\.ANNOUNCEMENTS_DATA\s*=\s*(.+?);?\s*$', js, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r'const\s+announcements\s*=\s*(.+?);?\s*$', js, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    raise ValueError(f"无法解析 {DATA_FILE} 的格式")


def save_data(data):
    data.sort(key=lambda x: x['date'], reverse=True)
    for i, ann in enumerate(data, 1):
        ann['id'] = i
    js = 'const announcements = ' + json.dumps(data, ensure_ascii=False, indent=2) + ';\n'
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        f.write(js)
    # Also save JSON for async loading
    json_file = os.path.join(os.path.dirname(DATA_FILE), 'announcements_data.json')
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clean_text(text):
    """清理公告内容，去掉页头页尾"""
    text = re.sub(r'(?s).*?(?=亲爱的玩家)', '', text, count=1)
    text = re.split(r'\[关于\S*网易大神', text)[0]
    text = re.split(r'扫描二维码', text)[0]
    text = re.split(r'公司简介', text)[0]
    text = re.split(r'页面报错反馈', text)[0]
    text = re.split(r'返回首页', text)[0]
    text = re.split(r'下载客户端', text)[0]
    text = re.sub(r'[\ue606\ue604\ue602\ue900]', '', text)
    text = '\n'.join([line.strip() for line in text.split('\n') if line.strip()])
    return text


def is_existing(data, url, title):
    """检查公告是否已存在"""
    url_normal = url.replace('https://', '').replace('http://', '')
    for a in data:
        if a.get('url'):
            existing = a['url'].replace('https://', '').replace('http://', '')
            if existing == url_normal:
                return True
        if a.get('title') == title:
            return True
    return False


def parse_date_from_title(title):
    """从标题提取日期 YYYY-MM-DD"""
    m = re.search(r'(\d{4})年(\d{2})月(\d{2})日', title)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# ───── 免费版（requests 抓取）─────

def fetch_free_list():
    """抓取免费版列表页，返回 [(服务器, 标题, 日期, URL)]"""
    results = []
    try:
        r = requests.get('https://dh2.163.com/news/update/', headers=HEADERS, timeout=30)
        r.raise_for_status()
        r.encoding = 'gbk'
        soup = BeautifulSoup(r.text, 'html.parser')

        for a in soup.find_all('a'):
            text = a.get_text(strip=True)
            href = a.get('href', '')

            if '维护公告' not in text or '预览' in text:
                continue

            # 提取标题（长文本中只有前部分是标题）
            title_match = re.match(r'(【常规服】\d{4}年\d{2}月\d{2}日维护公告)', text)
            if not title_match:
                title_match = re.match(r'(【怀旧服】\d{4}年\d{2}月\d{2}日维护公告)', text)
            if not title_match:
                continue
            title = title_match.group(1)
            date = parse_date_from_title(title)
            if not date:
                continue

            if title.startswith('【常规服】'):
                server = 'regular'
                server_name = '常规服'
            elif title.startswith('【怀旧服】'):
                server = 'nostalgic'
                server_name = '怀旧服'
            else:
                continue

            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = 'https://dh2.163.com' + href

            results.append({
                'server': server,
                'serverName': server_name,
                'title': title,
                'date': date,
                'url': href
            })

    except Exception as e:
        log(f'[ERROR] 免费版列表抓取失败: {e}')

    return results


def fetch_free_detail(url):
    """用 requests 抓取免费版详情页"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        r.encoding = 'gbk'
        soup = BeautifulSoup(r.text, 'html.parser')

        selectors = ['.content', '.news-content', '.article-content', '.detail-main', '.news_detail']
        text = ''
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator='\n', strip=True)
                if len(text) > 300:
                    break

        if not text or len(text) < 300:
            text = soup.body.get_text(separator='\n', strip=True) if soup.body else ''

        return clean_text(text)[:6000]
    except Exception as e:
        log(f'[ERROR] 免费版详情抓取失败 {url}: {e}')
        return ''


# ───── 经典版（Playwright stealth 抓取）─────

def fetch_classic_list():
    """抓取经典版列表页，返回 [(标题, 日期, URL)]"""
    results = []
    try:
        r = requests.get('https://xy2.163.com/news/update/', headers=HEADERS, timeout=30)
        r.raise_for_status()
        r.encoding = 'gbk'
        soup = BeautifulSoup(r.text, 'html.parser')

        for a in soup.find_all('a'):
            text = a.get_text(strip=True)
            href = a.get('href', '')

            if '停机维护公告' not in text or '预览' in text:
                continue

            title_match = re.search(r'(\d{4}年\d{2}月\d{2}日停机维护公告)', text)
            if not title_match:
                continue
            title = title_match.group(1)
            date = parse_date_from_title(title)
            if not date:
                continue

            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = 'https://xy2.163.com' + href

            results.append({
                'server': 'classic',
                'serverName': '经典版',
                'title': title,
                'date': date,
                'url': href
            })

    except Exception as e:
        log(f'[ERROR] 经典版列表抓取失败: {e}')

    return results


def fetch_classic_detail(url):
    """用 Playwright stealth 抓取经典版详情页"""
    text = ''
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled']
            )
            page = browser.new_page()
            page.add_init_script("delete navigator.webdriver;")
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(2500)

            selectors = ['.news-content', '.content-box', '.article-content', '.detail-main', '.news_detail', '.content']
            for sel in selectors:
                try:
                    text = page.inner_text(sel, timeout=5000)
                    if text and len(text) > 300:
                        break
                except:
                    pass

            if not text or len(text) < 300:
                text = page.evaluate('() => document.body.innerText')

            browser.close()
    except Exception as e:
        log(f'[ERROR] 经典版详情抓取失败 {url}: {e}')

    return clean_text(text)[:6000] if text else ''


# ───── 主流程 ─────

def main():
    log('=== 大话西游2 维护公告自动更新 ===')
    data = load_data()
    log(f'现有数据: {len(data)} 条')

    added = []

    # 1) 免费版
    free_items = fetch_free_list()
    log(f'免费版列表抓取到 {len(free_items)} 条')

    for item in free_items:
        if is_existing(data, item['url'], item['title']):
            log(f'[SKIP] 已存在: {item["title"]}')
            continue

        log(f'[FETCH] 免费版详情: {item["title"]}')
        content = fetch_free_detail(item['url'])
        if not content:
            log(f'[WARN] 内容为空，跳过')
            continue

        item['id'] = 0
        item['content'] = content
        data.append(item)
        added.append(item['title'])
        log(f'[ADDED] {item["title"]} ({len(content)} 字符)')

    # 2) 经典版
    classic_items = fetch_classic_list()
    log(f'经典版列表抓取到 {len(classic_items)} 条')

    for item in classic_items:
        if is_existing(data, item['url'], item['title']):
            log(f'[SKIP] 已存在: {item["title"]}')
            continue

        log(f'[FETCH] 经典版详情: {item["title"]}')
        content = fetch_classic_detail(item['url'])
        if not content:
            log(f'[WARN] 内容为空，跳过')
            continue

        item['id'] = 0
        item['content'] = content
        data.append(item)
        added.append(item['title'])
        log(f'[ADDED] {item["title"]} ({len(content)} 字符)')

    if added:
        save_data(data)
        log(f'\n成功添加 {len(added)} 条公告:')
        for t in added:
            log(f'  + {t}')
        log(f'更新后总计: {len(data)} 条')
        
        # ═══ 自动部署到 Cloudflare Pages ═══
        try:
            # 同步最新数据到 dist/ 目录
            json_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'announcements_data.json')
            dist_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist')
            json_dst = os.path.join(dist_dir, 'announcements_data.json')
            if os.path.exists(json_src) and os.path.exists(dist_dir):
                shutil.copy2(json_src, json_dst)
                log('已同步 announcements_data.json 到 dist/ 目录')
            
            # 调用 Cloudflare Pages 部署脚本
            deploy_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cf_deploy.py')
            if os.path.exists(deploy_script):
                log('正在自动部署到 Cloudflare Pages...')
                import subprocess
                result = subprocess.run([sys.executable, deploy_script], capture_output=True, text=True, timeout=300)
                log(result.stdout)
                if result.returncode != 0:
                    log(f'部署输出: {result.stderr}')
        except Exception as e:
            log(f'自动部署跳过: {e}')
        # ═══════════════════════════
    else:
        log('\n暂无新公告需要添加')

    return len(added)


if __name__ == '__main__':
    try:
        count = main()
        sys.exit(0 if count >= 0 else 1)
    except Exception as e:
        log(f'[FATAL] {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
