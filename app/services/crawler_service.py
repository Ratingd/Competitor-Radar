import asyncio
from sqlalchemy.orm import Session
from app.models import models
from app.services.ai_service import analyze_bidding
from datetime import datetime
import time
from playwright.sync_api import sync_playwright
import re
import queue
import threading

# 全局日志队列（线程安全，供 SSE 实时推送到前端）
log_queue = queue.Queue(maxsize=2000)

_crawl_resume_event = threading.Event()
_crawl_resume_event.set()

_active_crawl_thread_ids = set()
_active_crawl_thread_lock = threading.Lock()

def mark_crawl_thread_active():
    with _active_crawl_thread_lock:
        _active_crawl_thread_ids.add(threading.get_ident())

def unmark_crawl_thread_active():
    with _active_crawl_thread_lock:
        _active_crawl_thread_ids.discard(threading.get_ident())

def _is_crawl_thread():
    ident = threading.get_ident()
    with _active_crawl_thread_lock:
        return ident in _active_crawl_thread_ids

def pause_crawler():
    _crawl_resume_event.clear()
    push_log("收到暂停请求：爬虫将暂停在下一个安全点", "warning")

def resume_crawler():
    _crawl_resume_event.set()
    push_log("收到继续请求：爬虫将从暂停点恢复", "info")

def is_crawler_paused() -> bool:
    return not _crawl_resume_event.is_set()

def push_log(text: str, level: str = 'info'):
    """将日志写入队列并同时输出到 stdout"""
    if _is_crawl_thread() and not _crawl_resume_event.is_set():
        _crawl_resume_event.wait()
    import time as _time
    import builtins
    msg = {
        'type': 'log',
        'level': level,   # info / success / warning / error / crawl
        'text': text,
        'ts': _time.strftime('%H:%M:%S')
    }
    builtins.print(text, flush=True)
    try:
        log_queue.put_nowait(msg)
    except queue.Full:
        try:
            log_queue.get_nowait() # 丢弃最旧的
            log_queue.put_nowait(msg)
        except queue.Empty:
            pass

# 重写 print，使当前文件的所有 print 自动进入 push_log
import builtins
def print(*args, **kwargs):
    # 如果 kwargs 里有 flush 等，我们只取内容
    text = " ".join(str(arg) for arg in args)
    # 简单的分级逻辑
    level = 'info'
    if 'Error' in text or 'error' in text or 'Failed' in text or 'failed' in text:
        level = 'error'
    elif 'Success' in text or 'success' in text:
        level = 'success'
    push_log(text, level)


# 默认关键词（当未传入关键词时使用）
DEFAULT_KEYWORDS = ["广东省电信规划设计院有限公司", "华信咨询设计研究院有限公司", "广东南方电信规划咨询设计院有限公司", "中国移动通信集团设计院有限公司"]

def safe_goto(page, url, timeout=60000, max_retries=3):
    """
    带有重试机制的安全页面跳转
    """
    for attempt in range(max_retries):
        try:
            page.goto(url, timeout=timeout)
            return True
        except Exception as e:
            push_log(f"页面跳转失败 (尝试 {attempt + 1}/{max_retries}): {url} - {e}", 'warning')
            time.sleep(2)
    push_log(f"页面跳转最终失败: {url}", 'error')
    return False

# 当前使用的关键词（会被传入的关键词覆盖）
KEYWORDS = DEFAULT_KEYWORDS.copy()

# 中国移动招标网目标单位筛选
CMCC_TARGET_COMPANIES = ["广东", "广西", "海南", "互联网公司"]

# 中国电信阳光采购网目标省份
CHINATELECOM_TARGET_PROVINCE = "广东"

# 中国联通招标网目标省份
UNICOM_TARGET_PROVINCE = "广东"

# 广东省公共资源交易平台 - 无需额外筛选，本身就是广东省数据
GDZY_TARGET_PROVINCE = "广东"

# 海南省公共资源交易服务平台 - 无需额外筛选，本身就是海南省数据
HAINAN_TARGET_PROVINCE = "海南"

def clean_html(html_content):
    """
    简单清理 HTML 标签，保留纯文本，用于 AI 分析
    """
    if not html_content:
        return ""
    cleaned = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '\n', cleaned)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def process_bidding(db: Session, title: str, content: str, url: str, publish_date: datetime = None, notice_type: str = "中标公告", source_website: str = "广东省政府采购网", matched_competitors: list = None):
    """
    处理抓取到的数据：AI分析 -> 过滤 -> 存库
    """
    # 1. 查重
    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
    if existing:
        if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
             print(f"Updating existing record details (Missing or Incomplete): {title}", flush=True)
             pass 
        else:
            print(f"Skipping existing: {title}", flush=True)
            return existing

    # 2. AI 分析
    print(f"Analyzing: {title}...", flush=True)
    text_content = clean_html(content)
    
    # Safely truncate content
    content_to_analyze = text_content
    if content_to_analyze and len(content_to_analyze) > 10000:
        content_to_analyze = content_to_analyze[:10000]
        
    analysis = analyze_bidding(title, content_to_analyze, matched_competitors=matched_competitors)
    
    if analysis.get("is_agency_only") is True:
        print(f"Skipping: 竞对仅作为招标代理机构出现 - {title}", flush=True)
        return existing if existing else None
    
    if existing:
        existing.raw_html = content
        existing.meta_info = analysis
        existing.ai_score = 100  # Set default score to 100 since AI scoring is removed
        # Safely convert to string and slice
        summary_str = str(analysis.get('summary', ''))
        opp_analysis_str = str(analysis.get('opportunity_analysis', ''))
        
        existing.content_abstract = summary_str[:500]
        existing.category = str(analysis.get('category', '未分类'))
        existing.notice_type = notice_type 
        existing.source_website = source_website
        existing.opportunity_analysis = opp_analysis_str[:2000]
        if publish_date:
             existing.publish_date = publish_date
             
        db.commit()
        db.refresh(existing)
        print(f"Updated: {existing.title}", flush=True)
        return existing
    else:
        summary_str = str(analysis.get('summary', ''))
        opp_analysis_str = str(analysis.get('opportunity_analysis', ''))
        
        new_bidding = models.Bidding(
            title=title,
            source_url=url,
            publish_date=publish_date or datetime.now(),
            content_abstract=summary_str[:500],
            category=str(analysis.get('category', '未分类')),
            notice_type=notice_type,
            source_website=source_website,
            ai_score=100,  # Set default score to 100
            raw_html=content,
            meta_info=analysis,
            opportunity_analysis=opp_analysis_str[:2000]
        )
        db.add(new_bidding)
        db.commit()
        db.refresh(new_bidding)
        print(f"Saved: {new_bidding.title}", flush=True)
        return new_bidding

def crawl_guangdong(db: Session, context):
    """
    广东省政府采购网爬虫
    """
    print("\n=== Starting Guangdong Crawler ===", flush=True)
    base_url = "https://gdgpo.czt.gd.gov.cn/maincms-web/noticeInformationGd"
    page = context.new_page()
    
    try:
        print(f"Fetching list page: {base_url}...", flush=True)
        captured_items = []

        def handle_response(response):
            if "application/json" in response.headers.get("content-type", "") and "selectInfoForIndex" in response.url:
                try:
                    json_data = response.json()
                    data_list = []
                    if isinstance(json_data.get('data'), dict):
                            data_list = json_data.get('data').get('rows', [])
                    elif isinstance(json_data.get('data'), list):
                            data_list = json_data.get('data')
                    
                    if data_list and isinstance(data_list, list):
                        print(f"DEBUG: Found {len(data_list)} items in API: {response.url}", flush=True)
                        for item in data_list:
                            title = item.get('title') or item.get('noticeTitle') or item.get('subject') or item.get('name')
                            link = item.get('url') or item.get('link') or item.get('pageurl')
                            pub_time_str = item.get('publishTime') or item.get('noticeTime') or item.get('addtime')
                            pub_date = None
                            if pub_time_str:
                                try:
                                    if isinstance(pub_time_str, int): 
                                        pub_date = datetime.fromtimestamp(pub_time_str / 1000)
                                    else:
                                        if len(pub_time_str) >= 19:
                                                pub_date = datetime.strptime(pub_time_str[:19], "%Y-%m-%d %H:%M:%S")
                                        elif len(pub_time_str) >= 10:
                                                pub_date = datetime.strptime(pub_time_str[:10], "%Y-%m-%d")
                                except:
                                    pass
                            
                            if not link and item.get('id'):
                                if item.get('pageurl'):
                                    link = f"https://gdgpo.czt.gd.gov.cn{item.get('pageurl')}"
                                else:
                                    link = f"https://gdgpo.czt.gd.gov.cn/maincms-web/noticeGd?id={item.get('id')}"
                            
                            if title:
                                captured_items.append({
                                    "title": title, 
                                    "href": link or "https://gdgpo.czt.gd.gov.cn",
                                    "publish_date": pub_date
                                })
                except Exception as e:
                    if "Target page, context or browser has been closed" not in str(e):
                        print(f"Error parsing API response: {e}", flush=True)

        page.on("response", handle_response)
        if not safe_goto(page, base_url, timeout=60000):
            return
        
        notice_types = [
            {"name": "中标（成交）结果公告", "type": "中标（成交）结果公告"}
        ]
        
        for n_type in notice_types:
            type_name = n_type["name"]
            db_type = n_type["type"]
            push_log(f"\n--- 开始爬取类型: {type_name} ---", 'crawl')
            
            captured_items.clear()
            
            push_log(f"选择筛选条件: {type_name}", 'info')
            try:
                page.wait_for_selector(f"text={type_name}", timeout=10000)
                page.click(f"text={type_name}")
                time.sleep(1) 
            except Exception as e:
                push_log(f"选择筛选失败 '{type_name}': {e}", 'error')
                continue

            push_log("点击查询按钮...", 'info')
            try:
                page.wait_for_selector("text=查询", timeout=10000)
                with page.expect_response(lambda response: "selectInfoForIndex" in response.url and response.status == 200, timeout=15000) as response_info:
                    page.click("text=查询")
                push_log("第1页 API响应成功", 'info')
            except Exception as e:
                push_log(f"单击查询失败: {e}", 'error')

            time.sleep(1)
            push_log(f"第1页获取到 {len(captured_items)} 条公告", 'info')
            
            # 翻页获取更多数据
            for page_num in range(2, 4):  # 再爬2页
                try:
                    # 点击下一页
                    next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled)')
                    if next_btn:
                        next_btn.click()
                        time.sleep(3)
                        push_log(f"第{page_num}页获取到 {len(captured_items)} 条（累计）", 'info')
                    else:
                        push_log(f"没有更多分页，共{page_num-1}页", 'info')
                        break
                except Exception as e:
                    push_log(f"翻页失败: {e}", 'warning')
                    break
            
            push_log(f"翻页完成，{type_name} 共获取 {len(captured_items)} 条公告", 'info')
            
            if len(captured_items) == 0:
                    push_log("API截取无数据，尝试DOM解析...", 'warning')
                    time.sleep(3) 
                    try:
                        rows = page.locator("tr").all()
                        push_log(f"DOM中发现 {len(rows)} 行", 'info')
                        for row in rows:
                            row_text = row.inner_text()
                            if "标题" in row_text and "发布时间" in row_text:
                                continue
                            links = row.locator("a").all()
                            for link in links:
                                title = link.inner_text().strip()
                                href = link.get_attribute("href")
                                if title and href:
                                    if not href.startswith("http"):
                                        if href.startswith("/"):
                                            href = f"https://gdgpo.czt.gd.gov.cn{href}"
                                        else:
                                            href = f"https://gdgpo.czt.gd.gov.cn/maincms-web/{href}"
                                    if len(title) > 5:
                                        captured_items.append({"title": title, "href": href})
                                        break
                    except Exception as e:
                        push_log(f"DOM解析失败: {e}", 'error')

            for item in captured_items:
                title = item['title']
                url = item['href']
                pub_date = item.get('publish_date')
                
                if title and ("下载" in title or "指南" in title or "登录" in title):
                    continue

                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                        push_log(f"重新爬取不完整记录: {title[:50]}", 'info')
                    else:
                        if existing.notice_type != db_type:
                            existing.notice_type = db_type
                            db.commit()
                            push_log(f"更新公告类型: {title[:50]}", 'info')
                        push_log(f"已存在，跳过: {title[:50]}", 'info')
                        continue

                try:
                    detail_page = context.new_page()
                    detail_content = {"text": ""}
                    def handle_detail_response(response):
                        if "application/json" in response.headers.get("content-type", ""):
                            try:
                                if any(key in response.url for key in ["selectInfoByOpenTenderCode", "getInfoById", "getNoticeDetail"]):
                                    data = response.json()
                                    content_found = None
                                    stack = [data]
                                    while stack:
                                        current = stack.pop()
                                        if isinstance(current, dict):
                                            if 'content' in current and isinstance(current['content'], str) and len(current['content']) > 100:
                                                content_found = current['content']
                                                break 
                                            for k, v in current.items():
                                                if isinstance(v, (dict, list)):
                                                    stack.append(v)
                                        elif isinstance(current, list):
                                            for item in current:
                                                if isinstance(item, (dict, list)):
                                                    stack.append(item)
                                    if content_found:
                                        detail_content["text"] = content_found
                            except:
                                pass

                    detail_page.on("response", handle_detail_response)
                    if not safe_goto(detail_page, url, timeout=30000):
                        detail_page.close()
                        continue
                    try:
                        detail_page.wait_for_load_state("networkidle", timeout=15000)
                    except:
                        pass
                    
                    content = detail_content["text"]
                    if not content:
                        print("API interception failed, trying DOM fallback...", flush=True)
                        detail_page.wait_for_timeout(3000) 
                        if detail_content["text"]:
                            content = detail_content["text"]
                        else:
                            content = detail_page.evaluate("""() => {
                                const table = document.querySelector('table');
                                if (table) return table.outerHTML;
                                const contentDiv = document.querySelector('.content') || 
                                                    document.querySelector('.article') || 
                                                    document.querySelector('.notice-content') ||
                                                    document.querySelector('.noticeDetail') || 
                                                    document.querySelector('#app') || 
                                                    document.body;
                                return contentDiv.innerHTML; 
                            }""")
                    
                    # 检查是否包含竞对公司名称
                    content_str = str(content) if content else ""
                    matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                    
                    if matched_competitors:
                        push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                        process_bidding(db, title, content, url, pub_date, notice_type=db_type, source_website="广东省政府采购网", matched_competitors=matched_competitors) 
                    else:
                        title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                        print(f"Skipped (No competitor match): {title_safe[:50]}", flush=True)
                        
                    detail_page.close()
                except Exception as e:
                    print(f"Error processing detail {url}: {e}", flush=True)
                    try:
                        if 'detail_page' in locals():
                            detail_page.close()
                    except:
                        pass
    except Exception as e:
        print(f"Guangdong Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_guangxi(db: Session, context):
    """
    广西政府采购网爬虫 - 通过页面访问获取数据
    """
    # print("\n=== Starting Guangxi Crawler ===", flush=True)
    
    # 广西政府采购网主页
    base_url = "https://zfcg.gxzf.gov.cn/"
    
    # 定义需要抓取的类型和对应的URL参数
    notice_types = [
        {"name": "结果公告", "type": "中标（成交）结果公告", "url": "https://zfcg.gxzf.gov.cn/site/category?isProvince=true&districtCode=459900&parentId=66485&childrenCode=ZcyAnnouncement2"}
    ]
    
    page = context.new_page()
    
    try:
        for n_type in notice_types:
            type_name = n_type["name"]
            db_type = n_type["type"]
            list_url = n_type["url"]
            
            push_log(f"\n--- 开始爬取类型: {type_name} ---", 'crawl')
            # print(f"Fetching list page: {list_url}", flush=True)
            
            captured_items = []
            
            # API 拦截数据
            def handle_list_response(response):
                if "application/json" in response.headers.get("content-type", "") and "portal/category" in response.url:
                    try:
                        json_data = response.json()
                        data_list = []
                        if isinstance(json_data.get('result'), dict) and isinstance(json_data['result'].get('data'), dict):
                            data_list = json_data['result']['data'].get('data', [])
                        
                        if data_list and isinstance(data_list, list):
                            for item in data_list:
                                title = item.get('title')
                                article_id = item.get('articleId')
                                pub_time_str = item.get('publishDate')
                                pub_date = None
                                
                                if pub_time_str:
                                    try:
                                        if len(str(pub_time_str)) >= 10:
                                            pub_date = datetime.strptime(str(pub_time_str)[:10], "%Y-%m-%d")
                                    except:
                                        pass
                                
                                link = f"https://zfcg.gxzf.gov.cn/portal/detail?articleId={article_id}&parentId=66485" if article_id else ""
                                
                                if title and link:
                                    if not any(x['href'] == link for x in captured_items):
                                        captured_items.append({
                                            "title": title,
                                            "href": link,
                                            "publish_date": pub_date
                                        })
                    except Exception as e:
                        if "Target page, context or browser has been closed" not in str(e):
                            pass
            
            page.on("response", handle_list_response)
            
            try:
                # 访问列表页面
                page.goto(list_url, timeout=60000)
                try:
                    page.wait_for_load_state('networkidle', timeout=10000)
                except Exception:
                    pass
                time.sleep(2)  # 等待 API 响应和处理
                
                # 如果 API 拦截失败，尝试从页面中提取列表数据
                if len(captured_items) == 0:
                    items = page.evaluate("""() => {
                    const results = [];
                    // 尝试多种可能的选择器
                    const rows = document.querySelectorAll('.list-item, .notice-item, .article-item, tr');
                    rows.forEach(row => {
                        const link = row.querySelector('a');
                        if (link) {
                            const title = link.textContent?.trim();
                            let href = link.getAttribute('href');
                            if (title && href) {
                                // 处理相对链接
                                if (href.startsWith('/')) {
                                    href = 'https://zfcg.gxzf.gov.cn' + href;
                                } else if (!href.startsWith('http')) {
                                    href = 'https://zfcg.gxzf.gov.cn/' + href;
                                }
                                // 尝试获取日期
                                const dateEl = row.querySelector('.date, .time, .publish-date');
                                const date = dateEl ? dateEl.textContent.trim() : null;
                                results.push({title, href, date});
                            }
                        }
                    });
                    return results;
                }""")
                
                    if items and len(items) > 0:
                        push_log(f"DOM中发现 {len(items)} 行", 'info')
                        for item in items:
                            pub_date = None
                            if item.get('date'):
                                try:
                                    pub_date = datetime.strptime(str(item['date'])[:10], "%Y-%m-%d")
                                except:
                                    pass
                            captured_items.append({
                                "title": item['title'],
                                "href": item['href'],
                                "publish_date": pub_date
                            })
                    else:
                        push_log(f"第1页获取到 {len(captured_items)} 条（API拦截失败）", 'warning')
                else:
                    push_log(f"第1页获取到 {len(captured_items)} 条公告", 'info')

                # 翻页获取前三页数据
                for page_num in range(2, 4):
                    try:
                        next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled), li.number:not(.active)')
                        if next_btn:
                            with page.expect_response(lambda response: "application/json" in response.headers.get("content-type", "") and "portal/category" in response.url and response.status == 200, timeout=10000) as response_info:
                                next_btn.click()
                            time.sleep(2)
                            
                            push_log(f"第{page_num}页获取到 {len(captured_items)} 条（累计）", 'info')
                        else:
                            push_log(f"没有更多分页，共{page_num-1}页", 'info')
                            break
                    except Exception as e:
                        push_log(f"翻页失败: {e}", 'warning')
                        break
                
                # 移除 list_response 监听器
                page.remove_listener("response", handle_list_response)

                push_log(f"翻页完成，共获取 {len(captured_items)} 条公告", 'info')
                push_log(f"开始逐条处理 {len(captured_items)} 条公告详情...", 'info')
                
                # 处理抓取到的项目
                for item in captured_items:
                    title = item['title']
                    url = item['href']
                    pub_date = item.get('publish_date')
                    
                    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                    if existing:
                        if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                            push_log(f"重新爬取不完整记录: {title[:50]}", 'info')
                        else:
                            if existing.notice_type != db_type:
                                existing.notice_type = db_type
                                existing.source_website = "广西政府采购网"
                                db.commit()
                                push_log(f"更新公告类型: {title[:50]}", 'info')
                            push_log(f"已存在，跳过: {title[:50]}", 'info')
                            continue
                    
                    # 抓取详情
                    try:
                        detail_page = context.new_page()
                        # 准备拦截 API 响应
                        detail_content = {"text": ""}
                        def handle_detail_response(response):
                            if "application/json" in response.headers.get("content-type", ""):
                                try:
                                    data = response.json()
                                    content_found = None
                                    stack = [data]
                                    while stack:
                                        current = stack.pop()
                                        if isinstance(current, dict):
                                            if 'content' in current and isinstance(current['content'], str) and len(current['content']) > 100:
                                                content_found = current['content']
                                                break
                                            elif 'htmlContent' in current and isinstance(current['htmlContent'], str):
                                                content_found = current['htmlContent']
                                                break
                                            for k, v in current.items():
                                                if isinstance(v, (dict, list)):
                                                    stack.append(v)
                                        elif isinstance(current, list):
                                            for item in current:
                                                if isinstance(item, (dict, list)):
                                                    stack.append(item)
                                    if content_found:
                                        detail_content["text"] = content_found
                                except:
                                    pass

                        detail_page.on("response", handle_detail_response)
                        
                        # 优化页面加载速度，使用 domcontentloaded
                        detail_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        
                        # 循环检查是否已经拦截到了API响应，最多等 3 秒
                        wait_count = 0
                        while not detail_content["text"] and wait_count < 6:
                            time.sleep(0.5)
                            wait_count += 1
                            
                        if not detail_content["text"]:
                            try:
                                # 如果API没抓到，显式等待关键内容出现，缩短超时时间避免卡死
                                detail_page.wait_for_selector('table, .notice-area, .detail-content, .notice-detail, .article-detail', timeout=2000)
                            except:
                                pass

                        # 优先使用 API 拦截到的内容
                        content = detail_content["text"]
                        if not content:
                            # print("API interception failed/empty, using DOM extraction...", flush=True)
                            content = detail_page.evaluate("""() => {
                            // 1. 优先查找表格 (通常是意向公开的核心)
                            const table = document.querySelector('.notice-area table') || document.querySelector('table');
                            if (table) return table.outerHTML;
                            
                            // 2. 查找正文容器 (排除 header/footer)
                            const contentDiv = document.querySelector('.notice-area') || 
                                             document.querySelector('.detail-content') || 
                                             document.querySelector('.notice-detail');
                                             
                            if (contentDiv) return contentDiv.innerHTML;
                            
                            // 3. 兜底：如果实在找不到，尝试从 #app 中提取，但尽量移除干扰元素
                            const app = document.querySelector('#app') || document.body;
                            if (app) {
                                // 克隆节点以免影响页面
                                const clone = app.cloneNode(true);
                                // 移除头部、底部、侧边栏等常见干扰项
                                // 增加移除包含特定文本的元素 (如"下午好", "欢迎来到")
                                const toRemove = clone.querySelectorAll('.header, .footer, .sidebar, .top-bar, .bottom-bar, .breadcrumb, .nav, .menu, .logo-area, .search-area');
                                toRemove.forEach(el => el.remove());
                                
                                // 移除脚本和样式
                                clone.querySelectorAll('script, style').forEach(el => el.remove());
                                
                                // 尝试移除包含无关文本的顶部元素
                                // 这里简单遍历前几个子元素，如果是包含"下午好"的就移除
                                const children = Array.from(clone.children);
                                for (let i = 0; i < Math.min(children.length, 5); i++) {
                                    if (children[i].innerText && (children[i].innerText.includes('下午好') || children[i].innerText.includes('欢迎来到') || children[i].innerText.includes('登录'))) {
                                        children[i].remove();
                                    }
                                }
                                
                                return clone.innerHTML;
                            }
                            
                            return document.body.innerHTML;
                        }""")
                        
                        # 检查是否包含竞对公司名称
                        content_str = str(content) if content else ""
                        matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                        
                        if matched_competitors:
                            push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                            process_bidding(db, title, content, url, pub_date, notice_type=db_type, source_website="广西政府采购网", matched_competitors=matched_competitors)
                        else:
                            title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                            push_log(f"未命中竞对，跳过: {title_safe[:50]}", 'info')
                            
                        detail_page.close()
                    except Exception as e:
                        push_log(f"Error processing detail {url}: {e}", 'error')
                        try:
                            detail_page.close()
                        except:
                            pass
                        
            except Exception as e:
                push_log(f"Error processing category {type_name}: {e}", 'error')
                
    except Exception as e:
        push_log(f"Guangxi Crawler error: {e}", 'error')
    finally:
        page.close()

def run_crawler_task(db: Session):
    """运行所有网站的爬虫任务（向后兼容）"""
    run_crawler_task_for_websites(db, None, None)

def run_crawler_task_for_websites(db: Session, websites: list = None, keywords: list = None, email_config = None, feishu_webhook_url: str = None):
    """
    运行指定网站的爬虫任务
    
    Args:
        db: 数据库会话
        websites: 要爬取的网站ID列表，如 ["guangdong", "guangxi"]，None表示爬取所有
        keywords: 关键词列表，如 ["智算", "5G"]，None表示使用默认关键词
        email_config: 邮箱配置
        feishu_webhook_url: 飞书机器人 Webhook URL
    """
    mark_crawl_thread_active()
    _crawl_resume_event.set()

    # 设置关键词（如果传入了关键词则使用传入的，否则使用默认）
    global KEYWORDS
    if keywords and len(keywords) > 0:
        KEYWORDS = keywords
        push_log(f"使用自定义关键词（{len(KEYWORDS)}个）: {'、'.join(KEYWORDS)}", 'info')
    else:
        KEYWORDS = DEFAULT_KEYWORDS.copy()
        push_log(f"使用默认关键词（{len(KEYWORDS)}个）", 'info')
    
    push_log(f"开始爬虫任务，目标网站: {websites}", 'crawl')
    import asyncio
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    from sqlalchemy import func
    from app.models import models
    # 记录爬虫开始前的最大竞对动态 ID，用于极其精准地过滤出本次新抓取的动态
    # 彻底避开 SQLite 时间精度丢失和时区差异带来的 Bug
    max_id_record = db.query(func.max(models.Bidding.bid_id)).first()
    max_bid_id_before_task = max_id_record[0] if max_id_record and max_id_record[0] else 0
    
    # 默认爬取所有网站
    if websites is None:
            websites = ['guangdong', 'guangxi', 'cmcc', 'chinatelecom', 'shenzhen', 'guangzhou', 'unicom', 'gdzy', 'hainan', 'chinatower', 'gdzjcs', 'zycg', 'ccgp', 'dfmc', 'travelsky', 'powerchina', 'ceec']
    
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                
                # 根据选择的网站运行对应的爬虫
                if 'guangdong' in websites:
                    push_log('=== 开始爬取: 广东省政府采购网 ===', 'crawl')
                    crawl_guangdong(db, context)
                
                if 'guangxi' in websites:
                    push_log('=== 开始爬取: 广西政府采购网 ===', 'crawl')
                    crawl_guangxi(db, context)
                
                if 'cmcc' in websites:
                    push_log('=== 开始爬取: 中国移动招标与采购网 ===', 'crawl')
                    crawl_cmcc(db, context)
                
                if 'chinatelecom' in websites:
                    push_log('=== 开始爬取: 中国电信阳光采购网 ===', 'crawl')
                    crawl_chinatelecom(db, context)
                
                if 'shenzhen' in websites:
                    push_log('=== 开始爬取: 深圳市政府采购网 ===', 'crawl')
                    crawl_shenzhen(db, context)
                
                if 'guangzhou' in websites:
                    push_log('=== 开始爬取: 广州市政府采购中心 ===', 'crawl')
                    crawl_guangzhou(db, context)
                
                if 'unicom' in websites:
                    push_log('=== 开始爬取: 中国联通招标与采购网 ===', 'crawl')
                    crawl_unicom(db, context)
                
                if 'gdzy' in websites:
                    push_log('=== 开始爬取: 广东省公共资源交易平台 ===', 'crawl')
                    crawl_gdzy(db, context)
                
                if 'hainan' in websites:
                    push_log('=== 开始爬取: 海南省公共资源交易服务平台 ===', 'crawl')
                    crawl_hainan(db, context)
                    
                if 'chinatower' in websites:
                    push_log('=== 开始爬取: 中国铁塔电子采购平台 ===', 'crawl')
                    crawl_chinatower(db, context)
                
                if 'gdzjcs' in websites:
                    push_log('=== 开始爬取: 广东省网上中介服务超市 ===', 'crawl')
                    crawl_gdzjcs(db, context)
                
                if 'zycg' in websites:
                    push_log('=== 开始爬取: 中央政府采购网 ===', 'crawl')
                    crawl_zycg(db, context)
                
                if 'ccgp' in websites:
                    push_log('=== 开始爬取: 中国政府采购网 ===', 'crawl')
                    crawl_ccgp(db, context)
                
                if 'dfmc' in websites:
                    push_log('=== 开始爬取: 东风公司采购招投标平台 ===', 'crawl')
                    crawl_dfmc(db, context)
                
                if 'travelsky' in websites:
                    push_log('=== 开始爬取: 中国航信采购与招标网 ===', 'crawl')
                    crawl_travelsky(db, context)
                
                if 'powerchina' in websites:
                    push_log('=== 开始爬取: 中国电建阳光采购网 ===', 'crawl')
                    crawl_powerchina(db, context)
                
                if 'ceec' in websites:
                    push_log('=== 开始爬取: 中国能建电子采购平台 ===', 'crawl')
                    crawl_ceec(db, context)
                
            except Exception as e:
                push_log(f"浏览器启动失败: {e}", 'error')
            finally:
                if 'browser' in locals():
                    browser.close()
    finally:
        unmark_crawl_thread_active()

    push_log('所有网站抓取完成', 'success')

    # 如果配置了邮箱或飞书，获取本次新增数据并发送
    if email_config or feishu_webhook_url:
        push_log("正在整理竞对数据并发送通知...", "info")
        try:
            from app.models import models
            from app.services.report_service import generate_excel_bytes, generate_word_bytes, generate_email_html
            from app.services.email_service import send_report_email
            from app.services.feishu_service import send_to_feishu
            
            # 获取本次爬取到的数据
            biddings_for_notification = db.query(models.Bidding)\
                .filter(models.Bidding.bid_id > max_bid_id_before_task)\
                .order_by(models.Bidding.publish_date.desc())\
                .limit(100).all()
            
            # 发送邮件
            if email_config:
                excel_bytes = generate_excel_bytes(biddings_for_notification) if biddings_for_notification else None
                word_bytes = generate_word_bytes(biddings_for_notification) if biddings_for_notification else None
                html_content = generate_email_html(biddings_for_notification)
                
                success = send_report_email(email_config, excel_bytes, word_bytes, html_content)
                if success:
                    if biddings_for_notification:
                        push_log(f"本次新增 {len(biddings_for_notification)} 条竞对动态，已成功发送至邮箱！", "success")
                    else:
                        push_log("本次抓取无新增竞对动态，已发送零动态通知邮件。", "info")
                        
            # 发送飞书
            if feishu_webhook_url:
                success_feishu = send_to_feishu(feishu_webhook_url, biddings_for_notification)
                if success_feishu:
                    push_log("已成功发送通知至飞书群机器人！", "success")
                    
        except Exception as e:
            push_log(f"生成报告或发送通知过程出错: {e}", "error")

    push_log('整个爬虫任务链已全部结束', 'success')


def crawl_chinatower(db: Session, context):
    """
    中国铁塔电子采购平台爬虫
    按照用户要求，省份选择不限，增加抓取“候选人公示”和“采购结果公示”
    增强了异常处理，遇到单个类别错误时不中断整个流程
    """
    push_log("=== 开始爬取: 中国铁塔电子采购平台 ===", "crawl")
    
    target_urls = [
        ("候选人公示", "https://ebid.chinatowercom.cn/zgtt/gggs/003003/detailpage.html"),
        ("采购结果公示", "https://ebid.chinatowercom.cn/zgtt/gggs/003004/detailpage.html")
    ]
    
    page = context.new_page()
    captured_items = []
    
    try:
        for notice_type, url in target_urls:
            push_log(f"--- 正在抓取中国铁塔: {notice_type} ---", 'info')
            try:
                page.goto(url, timeout=60000)
                try:
                    page.wait_for_load_state('networkidle', timeout=15000)
                except:
                    pass
                time.sleep(2)
                
                # 点击查询 (不限省份和行业)
                try:
                    if page.locator(".chose-btn").count() > 0:
                        page.locator(".chose-btn").first.click(timeout=5000)
                        time.sleep(3)
                    else:
                        push_log(f"页面未完全加载或无查询按钮，直接尝试提取...", "warning")
                except Exception as e:
                    push_log(f"点击查询失败: {e}", "warning")
                    
                for page_num in range(1, 4):
                    items = page.evaluate("""() => {
                        const results = [];
                        const links = document.querySelectorAll('a');
                        for (const link of links) {
                            const title = link.innerText?.trim() || link.getAttribute('title');
                            let href = link.getAttribute('href');
                            if (title && title.length > 8 && href && href.includes('.html') && !href.includes('detailpage.html')) {
                                results.push({title: title, href: href});
                            }
                        }
                        return results;
                    }""")
                    
                    push_log(f"  {notice_type} 第 {page_num} 页找到 {len(items)} 条记录", "crawl")
                    import urllib.parse
                    for item in items:
                        href = item['href']
                        if not href.startswith('http'):
                            base = "https://ebid.chinatowercom.cn/zgtt/gggs/" + ("003003/" if notice_type == "候选人公示" else "003004/")
                            href = urllib.parse.urljoin(base, href)
                        if not any(x['href'] == href for x in captured_items):
                            captured_items.append({"title": item['title'], "href": href, "notice_type": notice_type})
                    
                    # 翻页
                    if page_num < 3 and len(items) > 0:
                        try:
                            push_log(f"  正在翻页到第 {page_num + 1} 页...", "crawl")
                            page.evaluate(f"""(pageNum) => {{
                                const els = Array.from(document.querySelectorAll('a'));
                                const target = els.find(e => e.innerText && e.innerText.trim() === String(pageNum));
                                if (target) {{
                                    target.click();
                                }} else {{
                                    const nextBtn = els.find(e => e.innerText && e.innerText.trim() === '下一页');
                                    if (nextBtn) nextBtn.click();
                                }}
                            }}""", page_num + 1)
                            time.sleep(5)
                        except Exception as e:
                            push_log(f"  翻页终止: {e}", "crawl")
                            break
            except Exception as e:
                push_log(f"抓取 {notice_type} 列表异常: {e}", "error")
                continue
                
        push_log(f"中国铁塔: 共抓取 {len(captured_items)} 条公告，正在进行正文匹配...", 'info')
        
        matched_count = 0
        total = len(captured_items)
        for idx, item in enumerate(captured_items, start=1):
            title = item['title']
            url = item['href']
            notice_type = item.get('notice_type', '采购结果公示')
            
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                continue
                
            try:
                push_log(f"中国铁塔 正文匹配 {idx}/{total}: {title[:60]}", "crawl")
                detail_page = context.new_page()
                detail_page.goto(url, timeout=30000)
                try:
                    detail_page.wait_for_load_state('networkidle', timeout=15000)
                except:
                    pass
                
                content = detail_page.evaluate("() => document.body.innerText")
                
                # 正文包含用户自定义关键词则爬取
                matched_competitors = [kw for kw in KEYWORDS if kw in content]
                if matched_competitors:
                    matched_count += 1
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    process_bidding(db, title, content, url, notice_type=notice_type, source_website="中国铁塔电子采购平台", matched_competitors=matched_competitors)
                
                detail_page.close()
            except Exception as e:
                push_log(f"详情页错误 {url}: {e}", "error")
                try:
                    detail_page.close()
                except:
                    pass
                    
        push_log(f"中国铁塔抓取完成，共命中竞对 {matched_count} 条", 'success' if matched_count > 0 else 'warning')
    except Exception as e:
        push_log(f"中国铁塔抓取总流程异常: {e}", 'error')
    finally:
        page.close()


def crawl_cmcc(db: Session, context):
    """
    中国移动招标与采购网爬虫
    只抓取中标公告下的“候选人公示”和“中选结果公示”，不限制单位
    抓取正文并进行竞对名称过滤
    """
    push_log("=== 开始爬取: 中国移动招标与采购网 ===", "crawl")
    
    base_url = "https://b2b.10086.cn/#/biddingProcurementBulletin"
    page = context.new_page()
    
    try:
        # 拦截API响应
        captured_items = []
        
        def handle_response(response):
            url = response.url
            if 'queryList' in url or 'publish/query' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        if isinstance(data, dict) and 'data' in data:
                            page_data = data['data']
                            if isinstance(page_data, dict) and 'content' in page_data:
                                records = page_data['content']
                                if isinstance(records, list):
                                    push_log(f"[API] 捕获到 {len(records)} 条列表数据", "crawl")
                                    for record in records:
                                        title = record.get('name')
                                        publish_id = record.get('id')
                                        uuid = record.get('uuid')
                                        notice_type = record.get('publishOneType_dictText') or record.get('publishType_dictText') or '中标公告'
                                        company = record.get('companyTypeName', '')
                                        
                                        # 只保留“候选人公示”和“中选结果公示”
                                        if notice_type not in ['候选人公示', '中选结果公示']:
                                            continue
                                            
                                        if title and publish_id:
                                            detail_url = f"https://b2b.10086.cn/#/noticeDetail?publishId={publish_id}&publishUuid={uuid or ''}"
                                            
                                            pub_date = None
                                            pub_time = record.get('publishDate') or record.get('backDate')
                                            if pub_time:
                                                try:
                                                    pub_date = datetime.strptime(str(pub_time)[:19], "%Y-%m-%d %H:%M:%S")
                                                except:
                                                    pass
                                            
                                            captured_items.append({
                                                'title': title,
                                                'url': detail_url,
                                                'publish_date': pub_date,
                                                'notice_type': notice_type,
                                                'company': company
                                            })
                except Exception as e:
                    if "Target page, context or browser has been closed" not in str(e):
                        push_log(f"[API Error] {e}", "error")
        
        page.on('response', handle_response)
        
        # 访问列表页
        push_log(f"正在访问列表页...", "crawl")
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(5)
        
        # 点击"候选人公示"
        try:
            push_log("点击 '候选人公示' 标签...", "crawl")
            # 使用包含匹配，兼容DOM结构嵌套
            try:
                page.wait_for_selector(".type-name", timeout=15000)
            except:
                pass
            page.evaluate('''() => {
                let els = Array.from(document.querySelectorAll('*'));
                let target = els.find(e => e.textContent && e.textContent.trim() === '候选人公示');
                if(target) target.click();
            }''')
            time.sleep(5)
            
            # 翻页获取"候选人公示"更多数据
            push_log("尝试获取更多页数据...", "crawl")
            for page_num in range(2, 4):  # 爬前3页
                try:
                    next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled)')
                    if next_btn:
                        push_log(f"正在翻页到第 {page_num} 页...", "crawl")
                        next_btn.click()
                        time.sleep(3)
                    else:
                        break
                except Exception as e:
                    push_log(f"翻页终止: {e}", "crawl")
                    break
        except Exception as e:
            push_log(f"点击 '候选人公示' 失败: {e}", "error")
            
        # 切换并点击"中选结果公示"
        try:
            push_log("点击 '中选结果公示' 标签...", "crawl")
            # 先点击第一页返回顶部
            try:
                page.locator(".el-pager li.number").first.click()
                time.sleep(2)
            except:
                pass
                
            page.evaluate('''() => {
                let els = Array.from(document.querySelectorAll('*'));
                let target = els.find(e => e.textContent && e.textContent.trim() === '中选结果公示');
                if(target) target.click();
            }''')
            time.sleep(5)
            
            # 翻页获取"中选结果公示"更多数据
            push_log("尝试获取更多页数据...", "crawl")
            for page_num in range(2, 4):  # 爬前3页
                try:
                    next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled)')
                    if next_btn:
                        push_log(f"正在翻页到第 {page_num} 页...", "crawl")
                        next_btn.click()
                        time.sleep(3)
                    else:
                        break
                except Exception as e:
                    push_log(f"翻页终止: {e}", "crawl")
                    break
        except Exception as e:
            push_log(f"点击 '中选结果公示' 失败: {e}", "error")
        
        push_log(f"共获取到 {len(captured_items)} 条待处理数据", "crawl")
        
        # 移除列表监听，防止干扰详情页
        page.remove_listener('response', handle_response)
        
        # 处理每条公告
        for item in captured_items:
            title = item['title']
            url = item['url']
            company = item.get('company', '')
            notice_type = item.get('notice_type', '中选结果公示')
            pub_date = item.get('publish_date')
            
            push_log(f"正在处理: {title[:60]}... [单位: {company}]", "crawl")
            
            # 检查是否已存在且完整
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                    pass
                else:
                    if existing.notice_type != notice_type:
                        existing.notice_type = notice_type
                        existing.source_website = f"中国移动招标与采购网-{company}"
                        db.commit()
                    push_log(f"  -> 已存在，跳过", "crawl")
                    continue
            
            # 抓取详情正文
            try:
                detail_page = context.new_page()
                detail_content = {"text": ""}
                
                def handle_detail_response(response):
                    if "application/json" in response.headers.get("content-type", ""):
                        try:
                            # 移动采购网的详情接口可能是 publish/detail 或相似的
                            if "publish/detail" in response.url or "publish/query" in response.url:
                                data = response.json()
                                # 尝试提取正文内容
                                content_found = None
                                stack = [data]
                                while stack:
                                    current = stack.pop()
                                    if isinstance(current, dict):
                                        if 'content' in current and isinstance(current['content'], str) and len(current['content']) > 100:
                                            content_found = current['content']
                                            break
                                        if 'noticeContent' in current and isinstance(current['noticeContent'], str) and len(current['noticeContent']) > 100:
                                            content_found = current['noticeContent']
                                            break
                                        for k, v in current.items():
                                            if isinstance(v, (dict, list)):
                                                stack.append(v)
                                    elif isinstance(current, list):
                                        for i in current:
                                            if isinstance(i, (dict, list)):
                                                stack.append(i)
                                                
                                if content_found:
                                    detail_content["text"] = content_found
                        except:
                            pass

                detail_page.on("response", handle_detail_response)
                detail_page.goto(url, timeout=30000)
                try:
                    detail_page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass
                
                content = detail_content["text"]
                if not content:
                    push_log("API 拦截失败，尝试提取 DOM 内容...", "crawl")
                    time.sleep(2)
                    content = detail_page.evaluate("""() => {
                        const contentDiv = document.querySelector('.notice-content') || 
                                         document.querySelector('.detail-content') || 
                                         document.querySelector('.ql-editor') ||
                                         document.querySelector('.notice-detail') ||
                                         document.querySelector('.content-body');
                        if (contentDiv) return contentDiv.innerHTML;
                        
                        // 移动采购网特有结构尝试
                        const frame = document.querySelector('iframe');
                        if (frame && frame.contentDocument) {
                            return frame.contentDocument.body.innerHTML;
                        }
                        
                        return document.body.innerHTML;
                    }""")
                
                # 检查正文是否包含竞对公司名称
                content_str = str(content) if content else ""
                matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                
                if matched_competitors:
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    process_bidding(db, title, content, url, pub_date, notice_type=notice_type, source_website=f"中国移动招标与采购网-{company}", matched_competitors=matched_competitors)
                else:
                    push_log(f"  -> 未命中竞对，跳过", "crawl")
                    
                detail_page.close()
            except Exception as e:
                push_log(f"处理详情页出错 {url}: {e}", "error")
                try:
                    detail_page.close()
                except:
                    pass
                
    except Exception as e:
        push_log(f"中国移动招标与采购网抓取失败: {e}", "error")
    finally:
        page.close()


def crawl_chinatelecom(db: Session, context):
    """
    中国电信阳光采购网爬虫
    全国数据抓取，公告类型选择“采购结果公示”
    抓取正文并进行竞对名称过滤
    """
    push_log("=== 开始爬取: 中国电信阳光采购网 ===", "crawl")
    
    base_url = "https://caigou.chinatelecom.com.cn/search"
    page = context.new_page()
    
    try:
        # 拦截API响应
        captured_items = []
        
        def handle_response(response):
            url = response.url
            if 'queryListNew' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        if isinstance(data, dict) and data.get('code') == 200:
                            inner_data = data.get('data', {})
                            page_info = inner_data.get('pageInfo', {})
                            records = page_info.get('list', [])
                            if isinstance(records, list):
                                push_log(f"[API] 捕获到 {len(records)} 条列表数据", "crawl")
                                for record in records:
                                    title = record.get('docTitle')
                                    province = record.get('provinceName', '')
                                    doc_type = record.get('docType', '')
                                    doc_type_code = record.get('docTypeCode', '')
                                    
                                    # 只处理采购结果公示 (ResultAnnounc)
                                    if doc_type_code != 'ResultAnnounc':
                                        continue
                                        
                                    notice_type = '采购结果公示'
                                        
                                    # 构造详情URL
                                    record_id = record.get('id', '')
                                    security_view_code = record.get('securityViewCode', '')
                                    
                                    if title and record_id:
                                        # 使用新的 DeclareDetails URL 格式
                                        detail_url = f"https://caigou.chinatelecom.com.cn/DeclareDetails?id={record_id}&type=7&docTypeCode={doc_type_code}&securityViewCode={security_view_code}"
                                        
                                        pub_date = None
                                        pub_time = record.get('createDate')
                                        if pub_time:
                                            try:
                                                pub_date = datetime.strptime(str(pub_time)[:10], "%Y-%m-%d")
                                            except:
                                                pass
                                        
                                        captured_items.append({
                                            'title': title,
                                            'url': detail_url,
                                            'publish_date': pub_date,
                                            'notice_type': notice_type,
                                            'province': province
                                        })
                except Exception as e:
                    if "Target page, context or browser has been closed" not in str(e):
                        pass
        
        page.on('response', handle_response)
        
        # 访问列表页
        push_log(f"正在访问列表页...", "crawl")
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(5)
        
        # 切换并点击"采购结果公示"
        try:
            push_log("点击 '采购结果公示' 标签...", "crawl")
            captured_items.clear()
            page.evaluate('''() => {
                let els = Array.from(document.querySelectorAll('*'));
                let target = els.find(e => e.textContent && e.textContent.trim() === '采购结果公示');
                if(target) target.click();
            }''')
            time.sleep(5)
        except Exception as e:
            push_log(f"点击 '采购结果公示' 失败: {e}", "error")
        
        # 尝试翻页获取更多数据
        push_log("尝试获取更多页数据...", "crawl")
        for page_num in range(2, 4):  # 翻3页
            try:
                next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled)')
                if next_btn:
                    push_log(f"正在翻页到第 {page_num} 页...", "crawl")
                    next_btn.click()
                    time.sleep(3)
                else:
                    page_btn = page.query_selector(f'.el-pager li:nth-child({page_num})')
                    if page_btn:
                        push_log(f"正在点击第 {page_num} 页...", "crawl")
                        page_btn.click()
                        time.sleep(3)
                    else:
                        break
            except Exception as e:
                push_log(f"翻页终止: {e}", "crawl")
                break
        
        push_log(f"共获取到 {len(captured_items)} 条待处理数据", "crawl")
        
        # 移除列表监听
        page.remove_listener('response', handle_response)
        
        # 处理每条公告
        for item in captured_items:
            title = item['title']
            url = item['url']
            province = item.get('province', '')
            notice_type = item.get('notice_type', '采购结果公示')
            pub_date = item.get('publish_date')
            
            push_log(f"正在处理: {title[:60]}... [省份: {province}]", "crawl")
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                    pass
                else:
                    if existing.notice_type != notice_type:
                        existing.notice_type = notice_type
                        existing.source_website = f"中国电信阳光采购网-{province}"
                        db.commit()
                    push_log(f"  -> 已存在，跳过", "crawl")
                    continue
            
            # 抓取详情正文
            try:
                detail_page = context.new_page()
                detail_content = {"text": ""}
                
                def handle_detail_response(response):
                    if "application/json" in response.headers.get("content-type", ""):
                        try:
                            # 尝试拦截获取正文的API (例如 queryNoticeDetail)
                            if "queryNoticeDetail" in response.url or "getNoticeDetail" in response.url:
                                data = response.json()
                                content_found = None
                                stack = [data]
                                while stack:
                                    current = stack.pop()
                                    if isinstance(current, dict):
                                        if 'content' in current and isinstance(current['content'], str) and len(current['content']) > 100:
                                            content_found = current['content']
                                            break
                                        if 'noticeContent' in current and isinstance(current['noticeContent'], str) and len(current['noticeContent']) > 100:
                                            content_found = current['noticeContent']
                                            break
                                        for k, v in current.items():
                                            if isinstance(v, (dict, list)):
                                                stack.append(v)
                                    elif isinstance(current, list):
                                        for i in current:
                                            if isinstance(i, (dict, list)):
                                                stack.append(i)
                                if content_found:
                                    detail_content["text"] = content_found
                        except:
                            pass

                detail_page.on("response", handle_detail_response)
                detail_page.goto(url, timeout=30000)
                try:
                    detail_page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass
                
                content = detail_content["text"]
                if not content:
                    # 页面直出或未拦截到API，尝试从DOM提取正文
                    time.sleep(2)
                    content = detail_page.evaluate("""() => {
                        const contentDiv = document.querySelector('.article-content') || 
                                         document.querySelector('.notice-content') || 
                                         document.querySelector('.detail-content') ||
                                         document.querySelector('.ql-editor') ||
                                         document.querySelector('.notice-detail') ||
                                         document.querySelector('.content-body') ||
                                         document.querySelector('.main-info') ||
                                         document.querySelector('.declare') ||
                                         document.querySelector('.a-content');
                        if (contentDiv) return contentDiv.innerHTML;
                        
                        // 扫描件iframe尝试
                        const frame = document.querySelector('iframe');
                        if (frame && frame.contentDocument) {
                            return frame.contentDocument.body.innerHTML;
                        }
                        
                        return document.body.innerHTML;
                    }""")
                
                # 检查正文是否包含竞对公司名称
                content_str = str(content) if content else ""
                matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                
                if matched_competitors:
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    process_bidding(db, title, content, url, pub_date, notice_type=notice_type, source_website=f"中国电信阳光采购网-{province}", matched_competitors=matched_competitors)
                else:
                    title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                    push_log(f"  -> 未命中竞对，跳过", "crawl")
                    
                detail_page.close()
            except Exception as e:
                push_log(f"处理详情页出错 {url}: {e}", "error")
                try:
                    detail_page.close()
                except:
                    pass
                
    except Exception as e:
        push_log(f"中国电信阳光采购网抓取失败: {e}", "error")
    finally:
        page.close()


def crawl_shenzhen(db: Session, context):
    """
    深圳市政府采购网爬虫
    直接访问中标结果公示列表页
    抓取正文并进行竞对名称过滤
    """
    push_log("=== 开始爬取: 深圳市政府采购网 ===", "crawl")
    
    base_url = "http://zfcg.szggzy.com:8081/gsgg/002001/002001004/002001004001/list.html"
    page = context.new_page()
    
    try:
        push_log(f"正在访问列表页...", "crawl")
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        captured_items = []
        
        # 翻页获取前3页数据
        for page_num in range(1, 4):
            push_log(f"正在获取第 {page_num} 页...", "crawl")
            
            # 提取当前页公告
            items = page.evaluate("""() => {
                const results = [];
                const listItems = document.querySelectorAll('ul.news-items li');
                for (const item of listItems) {
                    const link = item.querySelector('a.text-overflow');
                    if (link) {
                        const title = link.getAttribute('title') || link.innerText?.trim();
                        let href = link.getAttribute('href');
                        if (title && title.length > 5 && href) {
                            if (href.startsWith('/')) {
                                href = 'http://zfcg.szggzy.com:8081' + href;
                            }
                            const dateSpan = item.querySelector('span');
                            const date = dateSpan ? dateSpan.innerText?.trim() : '';
                            results.push({ title, href, date });
                        }
                    }
                }
                return results;
            }""")
            
            captured_items.extend(items)
            push_log(f"第 {page_num} 页获取到 {len(items)} 条公告", "crawl")
            
            # 点击下一页 (只有在不是最后一页时点击)
            if page_num < 3:
                try:
                    # 查找具体的页码数字链接来点击
                    next_btn = page.query_selector(f'.m-pagination-page a:has-text("{page_num + 1}")')
                    if next_btn:
                        next_btn.click()
                        time.sleep(3)
                    else:
                        # 兜底：尝试点击"下一页"按钮
                        next_btn = page.query_selector('.m-pagination-page a.next')
                        if next_btn:
                            next_btn.click()
                            time.sleep(3)
                        else:
                            break
                except Exception as e:
                    push_log(f"翻页终止: {e}", "crawl")
                    break
        
        push_log(f"共获取到 {len(captured_items)} 条待处理数据", "crawl")
        
        # 处理每条公告
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            push_log(f"正在处理: {title[:60]}...", "crawl")
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                    pass
                else:
                    if existing.notice_type != "中标结果公示":
                        existing.notice_type = "中标结果公示"
                        existing.source_website = "深圳市政府采购网"
                        db.commit()
                    push_log(f"  -> 已存在，跳过", "crawl")
                    continue
            
            # 解析日期
            pub_date = None
            if item.get('date'):
                try:
                    pub_date = datetime.strptime(str(item['date'])[:10], "%Y-%m-%d")
                except:
                    pass
            
            # 抓取详情页并判断竞对
            try:
                detail_page = context.new_page()
                detail_page.goto(url, timeout=30000)
                try:
                    detail_page.wait_for_load_state('networkidle', timeout=15000)
                except:
                    pass
                time.sleep(2)
                
                # 提取正文
                content = detail_page.evaluate("""() => {
                    const selectors = [
                        '.article-content', '.detail-content', '.news-content',
                        '.content-box', '.view-content', '.info-content',
                        '.article', '.content', '#content'
                    ];
                    
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText && el.innerText.length > 100) {
                            return el.innerHTML;
                        }
                    }
                    
                    // 查找包含大量文本的区域
                    const divs = document.querySelectorAll('div');
                    let bestMatch = null;
                    let maxTextLength = 0;
                    
                    for (const div of divs) {
                        const text = div.innerText;
                        if (text && text.length > maxTextLength && text.length > 300) {
                            if (div.id !== 'header' && !div.className.includes('nav') && !div.className.includes('footer')) {
                                maxTextLength = text.length;
                                bestMatch = div;
                            }
                        }
                    }
                    
                    return bestMatch ? bestMatch.innerHTML : document.body.innerHTML;
                }""")
                
                # 检查正文是否包含竞对公司名称
                content_str = str(content) if content else ""
                matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                
                if matched_competitors:
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    process_bidding(db, title, content, url, pub_date, notice_type="中标结果公示", source_website="深圳市政府采购网", matched_competitors=matched_competitors)
                else:
                    push_log(f"  -> 未命中竞对，跳过", "crawl")
                    
                detail_page.close()
                
            except Exception as e:
                push_log(f"处理详情页出错 {url}: {e}", "error")
                try:
                    detail_page.close()
                except:
                    pass
                
    except Exception as e:
        push_log(f"深圳市政府采购网抓取失败: {e}", "error")
    finally:
        page.close()


def crawl_guangzhou(db: Session, context):
    """
    广州市政府采购中心爬虫
    选择“结果公示”和“中标结果公告”
    详情页正文在iframe中，需要特殊处理
    抓取正文并根据竞对名称进行匹配
    """
    print("\n=== Starting Guangzhou Crawler ===", flush=True)
    
    base_url = "https://gzzfcg.gcycloud.cn/freecms/site/gzsaas/cggg/index.html"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        captured_items = []
        
        # 定义需要抓取的公告类型(页面上的文本)
        target_types = ["结果公示", "中标结果公告"]
        
        for t_type in target_types:
            print(f"\n--- Switching to type: {t_type} ---", flush=True)
            try:
                # 尝试点击左侧菜单
                page.evaluate(f"""() => {{
                    const links = Array.from(document.querySelectorAll('.noticeLeftUl li, .noticeLeftList li, .menu-item'));
                    const target = links.find(el => el.innerText.includes('{t_type}'));
                    if (target) target.click();
                }}""")
                time.sleep(3)
            except Exception as e:
                print(f"Error clicking type {t_type}: {e}", flush=True)
                continue
                
            # 提取当前类型的前3页数据
            for page_num in range(1, 4):
                print(f"  Page {page_num}...", flush=True)
                
                # 提取当前页公告
                items = page.evaluate(f"""() => {{
                    const results = [];
                    const listItems = document.querySelectorAll('.noticeShowList li, .noticeListUl li, .procurementAnnouncementShowList li, .news-list li');
                    for (const item of listItems) {{
                        const link = item.querySelector('a');
                        if (link) {{
                            const title = link.getAttribute('title') || link.innerText?.trim();
                            let href = link.getAttribute('href');
                            if (title && title.length > 5 && href && !href.includes('javascript')) {{
                                if (href.startsWith('/')) {{
                                    href = 'https://gzzfcg.gcycloud.cn' + href;
                                }}
                                const dateSpan = item.querySelector('.date, span.right');
                                const date = dateSpan ? dateSpan.innerText?.trim() : '';
                                results.push({{ title, href, date, type: '{t_type}' }});
                            }}
                        }}
                    }}
                    return results;
                }}""")
                
                captured_items.extend(items)
                print(f"  Found {len(items)} items", flush=True)
                
                # 翻页
                if page_num < 3:
                    try:
                        page.evaluate("""() => {
                            const links = Array.from(document.querySelectorAll('.pagination a, .page a, .btn-next'));
                            const target = links.find(a => a.innerText.includes('下一页') || a.innerText.includes('>'));
                            if (target) target.click();
                        }""")
                        time.sleep(3)
                    except Exception as e:
                        print(f"  Pagination error: {e}", flush=True)
                        break
                        
        print(f"\nTotal captured: {len(captured_items)} items", flush=True)
        
        # 去重
        unique_items = []
        seen_urls = set()
        for item in captured_items:
            if item['href'] not in seen_urls:
                seen_urls.add(item['href'])
                unique_items.append(item)
        
        # 处理每条公告
        for item in unique_items:
            title = item['title']
            url = item['href']
            n_type = item['type']
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                    pass
                else:
                    if existing.notice_type != n_type:
                        existing.notice_type = n_type
                        existing.source_website = "广州市政府采购中心"
                        db.commit()
                    print(f"  Skipping existing", flush=True)
                    continue
            
            # 解析日期
            pub_date = None
            if item.get('date'):
                try:
                    pub_date = datetime.strptime(str(item['date'])[:10], "%Y-%m-%d")
                except:
                    pass
            
            # 抓取详情页
            try:
                detail_page = context.new_page()
                detail_page.goto(url, timeout=30000)
                try:
                    detail_page.wait_for_load_state('networkidle', timeout=15000)
                except:
                    pass
                time.sleep(2)
                
                # 提取正文（可能在iframe中）
                content = detail_page.evaluate("""() => {
                    // 检查是否有iframe
                    const iframe = document.querySelector('iframe');
                    if (iframe) {
                        try {
                            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                            if (iframeDoc && iframeDoc.body) {
                                return iframeDoc.body.innerHTML;
                            }
                        } catch(e) {}
                    }
                    
                    // 尝试多种选择器
                    const selectors = ['.ggxx-con', '.ggxxCon', '.infoCon', '.content-box', '.detail-content', '.content'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText && el.innerText.length > 100) {
                            return el.innerHTML;
                        }
                    }
                    
                    // 查找包含大量文本的区域
                    const divs = document.querySelectorAll('div');
                    let bestMatch = null;
                    let maxTextLength = 0;
                    for (const div of divs) {
                        const text = div.innerText;
                        if (text && text.length > maxTextLength && text.length > 300) {
                            const className = (div.className || '').toString();
                            if (!className.includes('nav') && !className.includes('footer') && !className.includes('header')) {
                                maxTextLength = text.length;
                                bestMatch = div;
                            }
                        }
                    }
                    return bestMatch ? bestMatch.innerHTML : document.body.innerHTML;
                }""")
                
                # 检查正文是否包含竞对公司名称
                content_str = str(content) if content else ""
                matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                
                if matched_competitors:
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    process_bidding(db, title, content, url, pub_date, notice_type=n_type, source_website="广州市政府采购中心", matched_competitors=matched_competitors)
                else:
                    title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                    print(f"  Skipped (No competitor match): {title_safe[:50]}", flush=True)
                    
                detail_page.close()
                
            except Exception as e:
                print(f"  Detail page error: {e}", flush=True)
                try:
                    detail_page.close()
                except:
                    pass
                
    except Exception as e:
        print(f"Guangzhou Crawler error: {e}", flush=True)
    finally:
        page.close()


def crawl_unicom(db: Session, context):
    """
    中国联通招标与采购网爬虫
    全国抓取，公告类型选择“采购结果”
    获取正文并按竞对公司过滤
    """
    print("\n=== Starting ChinaUnicom Crawler ===", flush=True)
    
    base_url = "https://www.chinaunicombidding.cn/bidInformation"
    page = context.new_page()
    
    try:
        # 拦截API响应
        captured_data = []
        
        def handle_response(response):
            url = response.url
            if 'getAnnoList' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        records = data.get('data', {}).get('records', [])
                        if isinstance(records, list):
                            print(f"[API] Captured {len(records)} items", flush=True)
                            for r in records:
                                captured_data.append(r)
                except:
                    pass
        
        page.on('response', handle_response)
        
        # 访问列表页
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        # 设置筛选条件
        print("Setting filters...", flush=True)
        try:
            # 选择"采购结果"
            page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('.filter-item, span, a, div'));
                const target = links.find(el => el.innerText && el.innerText.trim() === '采购结果');
                if (target) target.click();
            }""")
            time.sleep(3)
        except Exception as e:
            print(f"Error clicking 采购结果: {e}", flush=True)
        
        # 翻页获取更多数据
        print("Getting more pages...", flush=True)
        for page_num in range(2, 4):  # 共3页
            try:
                # 查找下一页按钮
                next_btn = page.query_selector('.ant-pagination-next:not(.ant-pagination-disabled), .el-pagination .btn-next:not(.is-disabled)')
                if next_btn:
                    print(f"  Page {page_num}...", flush=True)
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
        
        print(f"Total captured: {len(captured_data)} items", flush=True)
        
        # 去重
        unique_items = []
        seen_ids = set()
        for item in captured_data:
            if item.get('id') not in seen_ids:
                seen_ids.add(item.get('id'))
                unique_items.append(item)
        
        # 处理每条公告
        for item in unique_items:
            province = item.get('provinceName', '')
            company = item.get('bidCompany', '')
            title = item.get('annoName', '')
            item_id = item.get('id')
            n_type = item.get('annoType', '采购结果')
            
            url = f"https://www.chinaunicombidding.cn/bidInformation/detail?id={item_id}"
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                    pass
                else:
                    if existing.notice_type != n_type:
                        existing.notice_type = n_type
                        existing.source_website = f"中国联通招标网-{province}"
                        db.commit()
                    print(f"  Skipping existing", flush=True)
                    continue
            
            # 抓取详情页
            try:
                detail_page = context.new_page()
                detail_page.goto(url, timeout=30000)
                try:
                    detail_page.wait_for_load_state('networkidle', timeout=15000)
                except:
                    pass
                time.sleep(2)
                
                # 提取正文
                content = detail_page.evaluate("""() => {
                    const selectors = ['.content', '.detail-content', '.content-box', '.article-content', '.infoCon'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText && el.innerText.length > 100) {
                            return el.innerHTML;
                        }
                    }
                    return document.body.innerHTML;
                }""")
                
                # 解析发布日期
                pub_date = None
                if item.get('createDate'):
                    try:
                        pub_date = datetime.strptime(str(item['createDate'])[:19], "%Y-%m-%d %H:%M:%S")
                    except:
                        pass
                
                # 检查正文是否包含竞对公司名称
                content_str = str(content) if content else ""
                matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                
                if matched_competitors:
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    process_bidding(db, title, content, url, pub_date, notice_type=n_type, source_website=f"中国联通招标网-{province}", matched_competitors=matched_competitors)
                else:
                    title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                    print(f"  Skipped (No competitor match): {title_safe[:50]}", flush=True)
                
                detail_page.close()
                
            except Exception as e:
                print(f"  Detail page error: {e}", flush=True)
                try:
                    detail_page.close()
                except:
                    pass
                
    except Exception as e:
        print(f"ChinaUnicom Crawler error: {e}", flush=True)
    finally:
        page.close()


def crawl_gdzy(db: Session, context):
    """
    广东省公共资源交易平台爬虫
    交易环节选择"中标结果"
    详情页正文抓取并按竞对公司进行过滤
    """
    print("\n=== Starting GDZY Crawler ===", flush=True)
    
    base_url = "https://ygp.gdzwfw.gov.cn/#/44/jygg"
    
    # 定义要爬取的类型
    notice_types = [
        {"parent": "工程建设", "name": "中标结果", "type": "中标结果公告"},
        {"parent": "政府采购", "name": "中标（成交）结果公告", "type": "中标结果公告"}
    ]
    
    for n_type in notice_types:
        parent_name = n_type["parent"]
        type_name = n_type["name"]
        db_type = n_type["type"]
        
        print(f"\n--- Processing type: {parent_name} -> {type_name} ---", flush=True)
        
        page = context.new_page()
        
        try:
            # 拦截API响应
            captured_items = []
            
            def handle_response(response):
                url = response.url
                if 'search/v2/items' in url:
                    try:
                        if 'json' in response.headers.get('content-type', ''):
                            data = response.json()
                            search_data = data.get('data', {})
                            page_data = search_data.get('pageData', [])
                            if isinstance(page_data, list):
                                print(f"[API] Captured {len(page_data)} items", flush=True)
                                for item in page_data:
                                    captured_items.append(item)
                    except:
                        pass
            
            page.on('response', handle_response)
            
            # 访问列表页
            print(f"Fetching list page...", flush=True)
            page.goto(base_url, timeout=60000)
            page.wait_for_load_state('networkidle', timeout=30000)
            time.sleep(3)
            
            # 设置筛选条件
            print("Setting filters...", flush=True)
            try:
                # 1. 先点击左侧的一级大类 (如：工程建设 或 政府采购)
                page.evaluate(f"""() => {{
                    const links = Array.from(document.querySelectorAll('.menu-item, .nav-item, li, a, span, div'));
                    const target = links.find(el => el.innerText && el.innerText.trim() === '{parent_name}');
                    if (target) target.click();
                }}""")
                time.sleep(3)
                
                # 2. 点击交易环节对应的二级选项 (如：中标结果 或 中标（成交）结果公告)
                page.evaluate(f"""() => {{
                    const links = Array.from(document.querySelectorAll('.filter-item, span, div, li, a'));
                    const target = links.find(el => el.innerText && el.innerText.trim() === '{type_name}');
                    if (target) target.click();
                }}""")
                time.sleep(5)
            except Exception as e:
                print(f"Error setting filter: {e}", flush=True)
            
            # 翻页获取更多数据
            print("Getting more pages...", flush=True)
            for page_num in range(2, 4):  # 共3页
                try:
                    # 广东省公共资源交易平台的翻页通过请求接口 search/v2/items 获取，
                    # UI层面的翻页我们可以通过点击相应的页码。
                    page.evaluate(f"""() => {{
                        const links = Array.from(document.querySelectorAll('.ant-pagination-item'));
                        const target = links.find(el => el.getAttribute('title') === '{page_num}' || (el.innerText && el.innerText.trim() === '{page_num}'));
                        if (target) target.click();
                    }}""")
                    print(f"  Page {page_num}...", flush=True)
                    time.sleep(5)
                except Exception as e:
                    print(f"  Pagination error: {e}", flush=True)
                    break
            
            print(f"Total captured for {type_name}: {len(captured_items)} items", flush=True)
            
            # 去重
            unique_items = []
            seen_ids = set()
            for item in captured_items:
                nid = item.get('noticeId')
                if nid and nid not in seen_ids:
                    seen_ids.add(nid)
                    unique_items.append(item)
            
            # 处理每条公告
            for item in unique_items:
                title = item.get('noticeTitle', '')
                notice_id = item.get('noticeId')
                
                if not title or not notice_id:
                    continue
                
                print(f"Processing: {title[:60]}...", flush=True)
                
                # 构造详情URL
                url = f"https://ygp.gdzwfw.gov.cn/#/44/new/jygg/v3/D?noticeId={notice_id}&projectCode={item.get('projectCode', '')}"
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                        pass
                    else:
                        if existing.notice_type != db_type:
                            existing.notice_type = db_type
                            existing.source_website = "广东省公共资源交易平台"
                            db.commit()
                        print(f"  Skipping existing", flush=True)
                        continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=30000)
                    try:
                        detail_page.wait_for_load_state('networkidle', timeout=15000)
                    except:
                        pass
                    time.sleep(2)
                    
                    # 提取正文
                    content = detail_page.evaluate("""() => {
                        // 检查是否有iframe
                        const iframe = document.querySelector('iframe');
                        if (iframe) {
                            try {
                                const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                                if (iframeDoc && iframeDoc.body) {
                                    return iframeDoc.body.innerHTML;
                                }
                            } catch(e) {}
                        }
                        
                        const selectors = ['.content', '.detail-content', '.info-content', '.article-content', '.article'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    # 解析发布日期
                    pub_date = None
                    pub_time = item.get('publishDate')
                    if pub_time:
                        try:
                            pub_date = datetime.strptime(str(pub_time)[:19], "%Y-%m-%d %H:%M:%S")
                        except:
                            pass
                    
                    # 检查正文是否包含竞对公司名称
                    content_str = str(content) if content else ""
                    matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                    
                    if matched_competitors:
                        push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                        source = item.get('source', '广东省公共资源交易平台')
                        process_bidding(db, title, content, url, pub_date, notice_type=db_type, source_website=source, matched_competitors=matched_competitors)
                    else:
                        title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                        print(f"  Skipped (No competitor match): {title_safe[:50]}", flush=True)
                        
                    detail_page.close()
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
                    
        except Exception as e:
            print(f"GDZY Crawler error for {type_name}: {e}", flush=True)
        finally:
            page.close()


def crawl_hainan(db: Session, context):
    """
    海南省公共资源交易服务平台爬虫
    交易信息选择"工程建设"下的"中标候选人公示"和"中标公告"，以及"政府采购"下的"采购结果公告"
    抓取正文并进行竞对名称过滤
    """
    print("\n=== Starting Hainan Crawler ===", flush=True)
    
    base_url = "https://ggzy.hainan.gov.cn/ggzyjy/jyxx/003001/003001001/jyxx_list.html"
    page = context.new_page()
    
    notice_types = [
        {"name": "中标候选人公示", "url": "https://ggzy.hainan.gov.cn/ggzyjy/jyxx/003001/003001005/jyxx_list.html"},
        {"name": "中标公告", "url": "https://ggzy.hainan.gov.cn/ggzyjy/jyxx/003001/003001006/jyxx_list.html"},
        {"name": "采购结果公告", "url": "https://ggzy.hainan.gov.cn/ggzyjy/jyxx/003002/003002004/jyxx_list.html"}
    ]
    
    try:
        captured_items = []
        current_type_name = ""
        
        def handle_response(response):
            url = response.url
            if 'getFullTextDataNew' in url or 'jyxx_list.html' in url or '/ggzyjy/' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        records = data.get('result', {}).get('records', [])
                        if isinstance(records, list):
                            print(f"[API] Captured {len(records)} items for {current_type_name}", flush=True)
                            for item in records:
                                item_copy = item.copy()
                                item_copy['categoryname'] = current_type_name
                                # deduplicate by info_id or linkurl
                                if not any(x.get('infoid') == item_copy.get('infoid') and x.get('linkurl') == item_copy.get('linkurl') for x in captured_items):
                                    captured_items.append(item_copy)
                except:
                    pass
        
        page.on('response', handle_response)
        
        for n_type in notice_types:
            type_name = n_type["name"]
            target_url = n_type["url"]
            current_type_name = type_name
            
            print(f"Fetching list page for {type_name}...", flush=True)
            page.goto(target_url, timeout=60000)
            page.wait_for_load_state('networkidle', timeout=30000)
            time.sleep(3)
            
            # 翻页获取数据
            print(f"Getting pages for {type_name}...", flush=True)
            for page_num in range(1, 4):  # 爬前3页
                if page_num > 1:
                    try:
                        print(f"  Page {page_num}...", flush=True)
                        page.evaluate(f"""(pageNum) => {{
                            const paginationEls = Array.from(document.querySelectorAll('a, li, span'));
                            const target = paginationEls.find(el => el.innerText && el.innerText.trim() === String(pageNum));
                            if (target) {{
                                target.click();
                            }} else {{
                                const nextBtn = document.querySelector('.pagination-next:not(.disabled), .next:not(.disabled), a.next');
                                if (nextBtn) nextBtn.click();
                            }}
                        }}""", page_num)
                        time.sleep(5)
                    except Exception as e:
                        print(f"  Pagination stopped: {e}", flush=True)
                        break

        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 移除监听
        page.remove_listener('response', handle_response)
        
        # 处理每条公告
        for item in captured_items:
            title = item.get('title', '')
            info_id = item.get('infoid')
            link_url = item.get('linkurl')
            notice_type = item.get('categoryname', '中标结果公告')
            
            if not title or (not info_id and not link_url):
                continue
                
            # 构造详情URL
            if link_url:
                url = f"https://ggzy.hainan.gov.cn{link_url}" if link_url.startswith('/') else link_url
            else:
                url = f"https://ggzy.hainan.gov.cn/ggzyjy/jyxx/003001/003001001/{info_id[:8]}/{info_id}.html"
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                continue
                
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 抓取详情页并匹配竞对公司
            try:
                detail_page = context.new_page()
                detail_page.goto(url, timeout=30000)
                detail_page.wait_for_load_state('networkidle', timeout=20000)
                time.sleep(2)
                
                # 提取正文
                content = detail_page.evaluate("""() => {
                    const selectors = ['.article', '.detail-content', '.content-box', '.info-content', '.ewb-art'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText && el.innerText.length > 100) {
                            return el.innerHTML;
                        }
                    }
                    // 尝试iframe
                    const frame = document.querySelector('iframe');
                    if (frame && frame.contentDocument) {
                        return frame.contentDocument.body.innerHTML;
                    }
                    return document.body.innerHTML;
                }""")
                
                content_str = str(content) if content else ""
                matched_competitors = [kw for kw in KEYWORDS if kw in content_str]
                
                if matched_competitors:
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    # 解析发布日期
                    pub_date = None
                    pub_time = item.get('webdate') or item.get('infodate')
                    if pub_time:
                        try:
                            pub_date = datetime.strptime(str(pub_time)[:19], "%Y-%m-%d %H:%M:%S")
                        except:
                            pass
                            
                    source = item.get('xiaquname', '海南省公共资源交易服务平台')
                    process_bidding(db, title, content, url, pub_date, notice_type=notice_type, source_website=source, matched_competitors=matched_competitors)
                else:
                    print(f"  Skipped (No competitor match)", flush=True)
                    
                detail_page.close()
            except Exception as e:
                print(f"  Detail page error: {e}", flush=True)
                try:
                    detail_page.close()
                except:
                    pass
                
    except Exception as e:
        print(f"Hainan Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_gdzjcs(db: Session, context):
    """
    广东省网上中介服务超市爬虫
    """
    print("\n=== Starting GDZJCS Crawler ===", flush=True)
    urls = [
        "https://ygp.gdzwfw.gov.cn/zjfwcs/gd-zjcs-pub/bidResultNotice/publicityList",
        "https://ygp.gdzwfw.gov.cn/zjfwcs/gd-zjcs-pub/bidResultNotice"
    ]
    page = context.new_page()
    
    try:
        captured_items = []
        for base_url in urls:
            print(f"Fetching list page: {base_url}...", flush=True)
            push_log(f"--- 正在抓取广东省网上中介服务超市 ---", 'info')
            try:
                page.goto(base_url, timeout=60000)
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(3)
            except Exception as e:
                print(f"Failed to load {base_url}: {e}", flush=True)
                continue
                
            # 翻页获取数据
            for page_num in range(1, 4):  # 抓取3页
                print(f"  Page {page_num}...", flush=True)
                
                if page_num > 1:
                    try:
                        page.evaluate(f"""(pageNum) => {{
                            const els = Array.from(document.querySelectorAll('li.number'));
                            const target = els.find(e => e.innerText && e.innerText.trim() === String(pageNum));
                            if (target) {{
                                target.click();
                            }} else {{
                                const nextBtn = document.querySelector('li.number:last-child');
                                if(nextBtn) nextBtn.click();
                            }}
                        }}""", page_num)
                        time.sleep(3)
                    except Exception as e:
                        print(f"  Pagination error: {e}", flush=True)
                        break
                
                items = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('a');
                    for(let link of links) {
                        const title = link.getAttribute('title') || link.innerText?.trim();
                        const href = link.getAttribute('href');
                        if(title && title.length > 5 && href && href.includes('/view/')) {
                            results.push({title: title, href: href});
                        }
                    }
                    return results;
                }""")
                
                print(f"  Found {len(items)} items on page {page_num}", flush=True)
                if not items:
                    break
                    
                import urllib.parse
                for item in items:
                    href = item['href']
                    if not href.startswith('http'):
                        if 'view/' in href:
                            view_id = href.split('view/')[-1]
                            href = f"https://ygp.gdzwfw.gov.cn/zjfwcs/gd-zjcs-pub/bidResultNotice/view/{view_id}"
                        else:
                            href = urllib.parse.urljoin('https://ygp.gdzwfw.gov.cn/zjfwcs/gd-zjcs-pub/bidResultNotice/', href)
                    if not any(x['href'] == href for x in captured_items):
                        captured_items.append({"title": item['title'], "href": href})
                        
        print(f"Total captured: {len(captured_items)} items", flush=True)
        push_log(f"广东省网上中介服务超市: 共抓取 {len(captured_items)} 条公告，正在进行正文匹配...", 'info')
        
        # 处理每条公告 (正文匹配)
        matched_count = 0
        for i, item in enumerate(captured_items, 1):
            title = item['title']
            url = item['href']
            
            print(f"[{i}/{len(captured_items)}] Processing: {title[:60]}...", flush=True)
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                print(f"  -> 已存在，跳过", flush=True)
                continue
            
            # 抓取详情页
            try:
                detail_page = context.new_page()
                detail_page.goto(url, timeout=30000)
                try:
                    detail_page.wait_for_load_state('networkidle', timeout=15000)
                except:
                    pass
                time.sleep(2)
                
                # 提取纯文本用于匹配，以及HTML用于保存
                content_text = detail_page.evaluate("() => document.body.innerText")
                
                if any(kw in content_text for kw in KEYWORDS):
                    content_html = detail_page.evaluate("""() => {
                        const selectors = ['.detail__main', '.content-wrap', '.notice-detail-wrap', '.detail-content', '.content-box', 'body'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    # 尝试从详情页提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date, .publish-time');
                        return timeEl ? timeEl.innerText : null;
                    }""")
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    matched_competitors = [kw for kw in KEYWORDS if kw in content_html]
                    if matched_competitors:
                        matched_count += 1
                        push_log(f"  -> 命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                        # 处理并保存
                        process_bidding(db, title, content_html, url, pub_date, notice_type="中标结果公告", source_website="广东省中介服务超市", matched_competitors=matched_competitors)
                else:
                    print(f"  -> 未命中竞对", flush=True)
                    push_log(f"  -> 未命中竞对，跳过: {title[:30]}...", 'info')
                
                detail_page.close()
            except Exception as e:
                print(f"  Detail page error {url}: {e}", flush=True)
                try:
                    detail_page.close()
                except:
                    pass
                    
        push_log(f"广东省中介服务超市抓取完成，共命中竞对 {matched_count} 条", 'success' if matched_count > 0 else 'warning')
                
    except Exception as e:
        push_log(f"广东省中介服务超市抓取异常: {e}", 'error')
        print(f"GDZJCS Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_zycg(db: Session, context):
    """
    中央政府采购网爬虫
    """
    print("\n=== Starting ZYCG Crawler ===", flush=True)
    # 中央政府采购网单独委托项目
    base_url = "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/ddwtxm/index.html"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        
        # 点击选择“中标（成交）公告”并等待数据加载
        try:
            page.click('.dropdown-menu1 li[names="中标（成交）公告"]', timeout=10000)
            page.wait_for_selector('#announcementDisplay li a', timeout=15000)
            time.sleep(2)
        except Exception as e:
            print(f"Wait for items failed: {e}", flush=True)
        
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            try:
                page.wait_for_selector('#announcementDisplay li a', timeout=10000)
            except:
                pass
                
            items = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('#announcementDisplay li a');
                for (const link of links) {
                    const title = link.getAttribute('title') || link.innerText?.trim();
                    let href = link.getAttribute('href');
                    if (title && href && title.length > 5) {
                        results.push({ title, href });
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('#nextPage')
                if next_btn:
                    disabled = page.evaluate("el => el.disabled", next_btn)
                    if disabled:
                        break
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        import urllib.parse
        for i, item in enumerate(captured_items):
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin('https://www.zycg.gov.cn', url)
            
            print(f"[{i+1}/{len(captured_items)}] Processing: {title[:60]}...", flush=True)
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                print(f"  -> Skipping existing", flush=True)
                continue
                
            # 抓取详情页
            try:
                detail_page = context.new_page()
                detail_page.goto(url, timeout=30000)
                detail_page.wait_for_load_state('networkidle', timeout=20000)
                time.sleep(2)
                
                # 提取正文
                content_html = detail_page.evaluate("""() => {
                    const selectors = ['.detail_content', '.article-content', '.content-box', 'body'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText && el.innerText.length > 100) {
                            return el.innerHTML;
                        }
                    }
                    return document.body.innerHTML;
                }""")
                
                # 尝试从详情页提取日期
                pub_date_str = detail_page.evaluate("""() => {
                    const timeEl = document.querySelector('.time, .date, .publish-time, .info-date');
                    if (timeEl) return timeEl.innerText;
                    
                    // 查找包含日期的文本
                    const allDivs = document.querySelectorAll('div, span, p');
                    for (const div of allDivs) {
                        const text = div.innerText;
                        if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50) {
                            return text;
                        }
                    }
                    return null;
                }""")
                
                detail_page.close()
                
                pub_date = None
                if pub_date_str:
                    import re
                    date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                    if date_match:
                        try:
                            pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                        except:
                            pass
                            
                if not pub_date:
                    pub_date = datetime.now()
                
                # 关键词匹配
                content_str = str(content_html) if content_html else ""
                matched_competitors = [kw for kw in KEYWORDS if kw in title or kw in content_str]
                if matched_competitors:
                    push_log(f"  -> 命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    # 处理并保存
                    process_bidding(db, title, content_html, url, pub_date, notice_type="中标公告", source_website="中央政府采购网", matched_competitors=matched_competitors)
                else:
                    print(f"  -> 未命中竞对", flush=True)
                    push_log(f"  -> 未命中竞对，跳过: {title[:30]}...", 'info')
                
            except Exception as e:
                print(f"  -> Detail page error: {e}", flush=True)
                try:
                    detail_page.close()
                except:
                    pass
                
    except Exception as e:
        print(f"ZYCG Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_ccgp(db: Session, context):
    """
    中国政府采购网爬虫 (包含中央公告和地方公告)
    """
    print("\n=== Starting CCGP Crawler ===", flush=True)
    urls = [
        {"url": "https://www.ccgp.gov.cn/cggg/zygg/zbgg/", "type": "中央公告"},
        {"url": "https://www.ccgp.gov.cn/cggg/dfgg/zbgg/", "type": "地方公告"}
    ]
    
    page = context.new_page()
    
    try:
        import urllib.parse
        for u_info in urls:
            base_url = u_info["url"]
            cg_type = u_info["type"]
            print(f"Fetching {cg_type} list page...", flush=True)
            
            page.goto(base_url, timeout=60000)
            time.sleep(5)
            
            captured_items = []
            
            # 翻页获取数据
            for page_num in range(1, 4):  # 抓取3页
                print(f"  Page {page_num}...", flush=True)
                
                items = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('ul.c_list_bid li a, .vT-srch-result-list-bid li a');
                    for (const link of links) {
                        const title = link.getAttribute('title') || link.innerText?.trim();
                        let href = link.getAttribute('href');
                        
                        if (title && href && title.length > 5) {
                            results.push({ title, href });
                        }
                    }
                    
                    if (results.length === 0) {
                        const links2 = document.querySelectorAll('a');
                        for (const link of links2) {
                            const title = link.getAttribute('title') || link.innerText?.trim();
                            let href = link.getAttribute('href');
                            if (title && href && href.includes('htm') && title.length > 5) {
                                results.push({ title, href });
                            }
                        }
                    }
                    return results;
                }""")
                
                print(f"  Found {len(items)} items on page {page_num}", flush=True)
                if not items:
                    break
                    
                for item in items:
                    if not any(x['href'] == item['href'] for x in captured_items):
                        captured_items.append(item)
                
                # 点击下一页
                try:
                    next_btn = page.query_selector('a.next:not(.disabled), a:has-text("下一页")')
                    if next_btn:
                        next_btn.click()
                        time.sleep(3)
                    else:
                        break
                except:
                    break
                    
            print(f"Total captured for {cg_type}: {len(captured_items)} items", flush=True)
            
            # 处理每条公告
            for item in captured_items:
                title = item['title']
                url = item['href']
                
                if not url.startswith('http'):
                    url = urllib.parse.urljoin(base_url, url)
                
                print(f"Processing: {title[:60]}...", flush=True)
                
                # 关键词匹配
                if any(kw in title for kw in KEYWORDS):
                    print(f"  Keyword matched", flush=True)
                    
                    # 检查是否已存在
                    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                    if existing:
                        print(f"  Skipping existing", flush=True)
                        continue
                    
                    # 抓取详情页
                    try:
                        detail_page = context.new_page()
                        detail_page.goto(url, timeout=60000, referer=base_url)
                        time.sleep(2)
                        
                        # 提取正文
                        content_html = detail_page.evaluate("""() => {
                            const selectors = ['.vF_detail_content', '.vT_detail_content', '.vF_deatil_main', '.vT_deatil_main', '.table', 'body'];
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && el.innerText && el.innerText.length > 100) {
                                    return el.innerHTML;
                                }
                            }
                            return document.body.innerHTML;
                        }""")
                        
                        # 尝试从详情页提取日期
                        pub_date_str = detail_page.evaluate("""() => {
                            const timeEl = document.querySelector('.time, .date, .publish-time, .vT_detail_content_content_text_t');
                            if (timeEl) return timeEl.innerText;
                            
                            const allDivs = document.querySelectorAll('div, span, p');
                            for (const div of allDivs) {
                                const text = div.innerText;
                                if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50) {
                                    return text;
                                }
                            }
                            return null;
                        }""")
                        
                        detail_page.close()
                        
                        pub_date = None
                        if pub_date_str:
                            import re
                            date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                            if date_match:
                                try:
                                    pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                                except:
                                    pass
                                    
                        if not pub_date:
                            pub_date = datetime.now()
                        
                        matched_competitors = [kw for kw in KEYWORDS if kw in content_html]
                        if matched_competitors:
                            push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                            # 处理并保存
                            process_bidding(db, title, content_html, url, pub_date, notice_type="中标公告", source_website=f"中国政府采购网({cg_type})", matched_competitors=matched_competitors)
                        
                    except Exception as e:
                        print(f"  Detail page error: {e}", flush=True)
                        try:
                            detail_page.close()
                        except:
                            pass
                else:
                    print(f"  No keyword match, skipped", flush=True)
                    
    except Exception as e:
        print(f"CCGP Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_dfmc(db: Session, context):
    """
    东风公司采购招投标平台爬虫
    使用 etp.dfmc.com.cn 绕过 dfmjyzx.com 的长亭雷池 WAF 防护
    """
    print("\n=== Starting DFMC Crawler ===", flush=True)
    push_log(f"--- 正在抓取东风公司采购招投标平台 ---", 'info')
    base_url = "https://etp.dfmc.com.cn/jyxx/004001/trade_info_new.html"
    page = context.new_page()
    
    # 注入绕过 webdriver 检测
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        time.sleep(3)
        
        try:
            # 点击“中标公告”分类
            zb_btn = page.query_selector('text="中标公告"')
            if zb_btn:
                zb_btn.click()
                time.sleep(3)
        except Exception as e:
            print("Click 中标公告 failed:", e)
            
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            items = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('table tbody tr td a, .info_list li a, .list_ul li a, ul.ewb-right-item li a, a[title]');
                for (const link of links) {
                    const title = link.getAttribute('title') || link.innerText?.trim();
                    let href = link.getAttribute('href');
                    if (title && href && href.includes('html') && title.length > 5) {
                        results.push({ title, href });
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('.next, li.next a, a:has-text("下一页"), a:has-text(">")')
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        push_log(f"东风公司采购招投标平台: 共抓取 {len(captured_items)} 条公告，正在进行正文匹配...", 'info')
        
        # 处理每条公告
        import urllib.parse
        matched_count = 0
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin("https://etp.dfmc.com.cn", url)
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                print(f"  Skipping existing", flush=True)
                continue
            
            # 抓取详情页
            try:
                detail_page = context.new_page()
                detail_page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """)
                detail_page.goto(url, timeout=60000, referer=base_url)
                time.sleep(2)
                
                # 提取纯文本用于匹配
                content_text = detail_page.evaluate("() => document.body.innerText")
                
                # 正文匹配
                if any(kw in content_text for kw in KEYWORDS):
                    print(f"  Keyword matched in content", flush=True)
                    matched_count += 1
                    
                    # 提取正文 HTML
                    content_html = detail_page.evaluate("""() => {
                        const selectors = ['.public-content', '.article-info', '.notice-detail', '.article_con', '.content_box', '.detail_main', 'body'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    # 尝试从详情页提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date, .publish-time, .vT_detail_content_content_text_t');
                        if (timeEl) return timeEl.innerText;
                        
                        const allDivs = document.querySelectorAll('div, span, p');
                        for (const div of allDivs) {
                            const text = div.innerText;
                            if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && text.includes('发布时间')) {
                                return text;
                            }
                        }
                        return null;
                    }""")
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    matched_competitors = [kw for kw in KEYWORDS if kw in content_html]
                    if matched_competitors:
                        push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                        # 处理并保存
                        process_bidding(db, title, content_html, url, pub_date, notice_type="中标公告", source_website="东风公司采购招投标平台", matched_competitors=matched_competitors)
                else:
                    print(f"  No keyword match in content, skipped", flush=True)
                    
            except Exception as e:
                print(f"  Detail page error: {e}", flush=True)
            finally:
                try:
                    detail_page.close()
                except:
                    pass
                    
        push_log(f"东风公司采购招投标平台抓取完成，共命中竞对 {matched_count} 条", 'success' if matched_count > 0 else 'warning')
                
    except Exception as e:
        push_log(f"东风公司采购招投标平台抓取异常: {e}", 'error')
        print(f"DFMC Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_travelsky(db: Session, context):
    """
    中国航信采购与招标网爬虫
    注意处理正文在独立 iframe 中的情况
    """
    print("\n=== Starting Travelsky Crawler ===", flush=True)
    push_log(f"--- 正在抓取中国航信采购与招标网 ---", 'info')
    base_url = "http://gys.travelsky.com.cn/travelsky/noticeTenderingResult/noticeTenderingResultHtml"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        time.sleep(3)
        
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            items = page.evaluate("""() => {
                const results = [];
                const as = document.querySelectorAll('a[href*="getCaiGouId"]');
                for (const a of as) {
                    const title = a.innerText?.trim() || a.getAttribute('title');
                    const href = a.getAttribute('href');
                    if (title && href && href.includes('getCaiGouId')) {
                        const match = href.match(/getCaiGouId\\((\\d+)\\)/);
                        if (match) {
                            results.push({
                                title: title.replace(/\\s+/g, ' '),
                                href: '/travelsky/noticeTenderingResult/selectHtml/' + match[1]
                            });
                        }
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('div[name="whj_nextPage"]')
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        push_log(f"中国航信采购与招标网: 共抓取 {len(captured_items)} 条公告，正在进行正文匹配...", 'info')
        
        # 处理每条公告
        import urllib.parse
        matched_count = 0
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin("http://gys.travelsky.com.cn", url)
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 检查是否已存在
            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
            if existing:
                print(f"  Skipping existing", flush=True)
                continue
            
            # 抓取详情页
            try:
                detail_page = context.new_page()
                detail_page.goto(url, timeout=60000, referer=base_url)
                time.sleep(3)
                
                # 尝试从 iframe 中提取正文文本用于匹配
                content_text = ""
                content_html = ""
                try:
                    frames = detail_page.frames
                    for f in frames:
                        if f.url == 'about:blank' or 'travelsky' not in f.url: # Usually the content is in an about:blank iframe
                            text = f.evaluate("() => document.body.innerText")
                            if text and len(text) > 50:
                                content_text = text
                                content_html = f.evaluate("() => document.body.innerHTML")
                                break
                except Exception as e:
                    print(f"  Iframe extract error: {e}", flush=True)
                
                if not content_text:
                    content_text = detail_page.evaluate("""() => {
                        const selectors = ['.notice_body', '.content', 'body'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 50) {
                                return el.innerText;
                            }
                        }
                        return document.body.innerText;
                    }""")
                    
                if not content_html:
                    content_html = detail_page.evaluate("""() => {
                        const selectors = ['.notice_body', '.content', 'body'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 50) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                
                # 正文匹配
                matched_competitors = [kw for kw in KEYWORDS if kw in content_text]
                if matched_competitors:
                    print(f"  Keyword matched in content", flush=True)
                    matched_count += 1
                    
                    # 提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date');
                        if (timeEl) return timeEl.innerText;
                        
                        const allDivs = document.querySelectorAll('div, span, p');
                        for (const div of allDivs) {
                            const text = div.innerText;
                            if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && text.includes('发布时间')) {
                                return text;
                            }
                        }
                        return null;
                    }""")
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    # 处理并保存
                    push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                    process_bidding(db, title, content_html, url, pub_date, notice_type="采购结果公告", source_website="中国航信采购与招标网", matched_competitors=matched_competitors)
                else:
                    print(f"  No keyword match in content, skipped", flush=True)
                    
            except Exception as e:
                print(f"  Detail page error: {e}", flush=True)
            finally:
                try:
                    detail_page.close()
                except:
                    pass
                    
        push_log(f"中国航信采购与招标网抓取完成，共命中竞对 {matched_count} 条", 'success' if matched_count > 0 else 'warning')
                
    except Exception as e:
        push_log(f"中国航信采购与招标网抓取异常: {e}", 'error')
        print(f"Travelsky Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_powerchina(db: Session, context):
    """
    中国电建阳光采购网爬虫
    分别获取中标候选人公示和中标/成交公示，并提取正文进行竞对匹配
    """
    print("\n=== Starting PowerChina Crawler ===", flush=True)
    push_log(f"--- 正在抓取中国电建阳光采购网 ---", 'info')
    base_url = "https://bid.powerchina.cn/consult/publicity"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        try:
            page.wait_for_load_state('networkidle', timeout=30000)
        except Exception as e:
            print(f"  networkidle timeout, continuing: {e}", flush=True)
        time.sleep(5)
        
        captured_items = []
        matched_count = 0
        
        categories = ["中标候选人公示", "中标/成交公示"]
        
        for category in categories:
            print(f"Processing category: {category}", flush=True)
            push_log(f"中国电建阳光采购网: 正在抓取分类 '{category}'", 'info')
            
            # 点击分类
            try:
                page.evaluate(f"""(catName) => {{
                    const items = Array.from(document.querySelectorAll('div, li, span, a')).filter(el => el.innerText && el.innerText.trim() === catName);
                    if (items.length > 0) items[items.length - 1].click();
                }}""", category)
                time.sleep(5)
            except Exception as e:
                print(f"  Error clicking category {category}: {e}", flush=True)
                continue
            
            # 翻页获取数据
            for page_num in range(1, 2):  # 抓取1页
                print(f"  Page {page_num}...", flush=True)
                
                row_count = page.evaluate("() => document.querySelectorAll('.el-table__row').length")
                print(f"  Found {row_count} rows on page {page_num}", flush=True)
                
                if row_count == 0:
                    break
                    
                for i in range(row_count):
                    try:
                        title = page.evaluate(f"() => {{ const el = document.querySelectorAll('.el-table__row')[{i}].querySelector('.title, .name, a, .cell span'); return el ? el.innerText.trim() : ''; }}")
                        
                        if not title or len(title) < 5:
                            continue
                            
                        print(f"Processing: {title[:60]}...", flush=True)
                        
                        # 点击该行
                        with context.expect_page(timeout=15000) as new_page_info:
                            page.evaluate(f"""() => {{
                                const row = document.querySelectorAll('.el-table__row')[{i}];
                                const link = row.querySelector('.title, a');
                                if (link) link.click();
                                else row.click();
                            }}""")
                        
                        detail_page = new_page_info.value
                        url = detail_page.url
                        
                        # 检查是否已存在
                        existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                        if existing:
                            print(f"  Skipping existing", flush=True)
                            detail_page.close()
                            continue
                            
                        try:
                            detail_page.wait_for_load_state('networkidle', timeout=15000)
                        except:
                            pass
                        time.sleep(3)
                        
                        # 提取正文文本和HTML
                        content_data = detail_page.evaluate("""() => {
                            let text = "";
                            let html = "";
                            const iframe = document.querySelector('iframe');
                            if (iframe) {
                                try {
                                    text = iframe.contentDocument.body.innerText;
                                    html = iframe.contentDocument.body.innerHTML;
                                    if (text && text.length > 50) return {text, html};
                                } catch(e) {
                                    // ignore CORS error
                                }
                            }
                            const selectors = ['.content-box', '.notice-content', '.detail-content', '.ql-editor', '.content'];
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && el.innerText && el.innerText.length > 50) {
                                    return {text: el.innerText, html: el.innerHTML};
                                }
                            }
                            return {text: document.body.innerText, html: document.body.innerHTML};
                        }""")
                        
                        content_text = content_data.get('text', '')
                        content_html = content_data.get('html', '')
                        
                        # 正文匹配
                        matched_competitors = [kw for kw in KEYWORDS if kw in content_text]
                        if matched_competitors:
                            print(f"  Keyword matched in content", flush=True)
                            matched_count += 1
                            
                            # 提取日期
                            pub_date_str = detail_page.evaluate("""() => {
                                const timeEl = document.querySelector('.time, .date');
                                if (timeEl) return timeEl.innerText;
                                
                                const allDivs = document.querySelectorAll('div, span, p');
                                for (const div of allDivs) {
                                    const text = div.innerText;
                                    if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && text.includes('发布时间')) {
                                        return text;
                                    }
                                }
                                return null;
                            }""")
                            
                            pub_date = None
                            if pub_date_str:
                                import re
                                date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                                if date_match:
                                    try:
                                        pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                                    except:
                                        pass
                                        
                            if not pub_date:
                                pub_date = datetime.now()
                            
                            # 处理并保存
                            push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                            process_bidding(db, title, content_html, url, pub_date, notice_type=category, source_website="中国电建阳光采购网", matched_competitors=matched_competitors)
                            captured_items.append({"title": title, "url": url})
                        else:
                            print(f"  No keyword match in content, skipped", flush=True)
                            
                    except Exception as e:
                        print(f"  Error processing row {i}: {e}", flush=True)
                    finally:
                        try:
                            detail_page.close()
                        except:
                            pass
                
                # 由于只抓取第一页，不需要点击下一页
                break
                    
        print(f"Total captured: {len(captured_items)} items", flush=True)
        push_log(f"中国电建阳光采购网抓取完成，共命中竞对 {matched_count} 条", 'success' if matched_count > 0 else 'warning')
                
    except Exception as e:
        push_log(f"中国电建阳光采购网抓取异常: {e}", 'error')
        print(f"PowerChina Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_ceec(db: Session, context):
    """
    中国能建电子采购平台爬虫
    分别获取候选人公告专区和中标公示专区，并提取正文进行竞对匹配
    """
    print("\n=== Starting CEEC Crawler ===", flush=True)
    push_log(f"--- 正在抓取中国能建招标采购平台 ---", 'info')
    
    urls = [
        {"url": "https://ceec.dnezb.com/3019", "type": "候选人公告"},
        {"url": "https://ceec.dnezb.com/3011", "type": "中标公示"}
    ]
    
    page = context.new_page()
    matched_count = 0
    
    try:
        for target in urls:
            base_url = target["url"]
            notice_type = target["type"]
            print(f"Fetching list page {base_url} ({notice_type})...", flush=True)
            push_log(f"中国能建: 正在抓取分类 '{notice_type}'", 'info')
            
            page.goto(base_url, timeout=60000)
            try:
                page.wait_for_load_state('networkidle', timeout=30000)
            except Exception as e:
                print(f"  networkidle timeout, continuing: {e}", flush=True)
            time.sleep(3)
            
            captured_items = []
            seen_hrefs = set()
            
            # 翻页获取数据
            for page_num in range(1, 4):  # 抓取3页
                print(f"  Page {page_num}...", flush=True)
                
                items = page.evaluate("""() => {
                    const links = document.querySelectorAll('a');
                    const results = [];
                    for (const a of links) {
                        const href = a.getAttribute('href');
                        const title = a.innerText?.trim() || a.getAttribute('title');
                        
                        if (href && href.includes('detail') && title && title.length > 5) {
                            results.push({title: title, href: href});
                        }
                    }
                    return results;
                }""")
                
                print(f"  Found {len(items)} items on page {page_num}", flush=True)
                if not items:
                    break
                    
                for item in items:
                    href = item.get('href')
                    if href and href not in seen_hrefs:
                        seen_hrefs.add(href)
                        captured_items.append(item)
                
                # 点击下一页
                if page_num < 3:
                    try:
                        has_next = page.evaluate("""() => {
                            const nextBtns = Array.from(document.querySelectorAll('a, button, li')).filter(el => el.innerText && (el.innerText.includes('下一页') || el.innerText.includes('次页') || el.innerText.includes('>')));
                            if (nextBtns.length > 0) {
                                nextBtns[nextBtns.length - 1].click();
                                return true;
                            }
                            
                            // check pagination ul
                            const ul = document.querySelector('.el-pager, .pagination');
                            if (ul) {
                                const nextBtn = ul.nextElementSibling;
                                if (nextBtn && !nextBtn.disabled) {
                                    nextBtn.click();
                                    return true;
                                }
                            }
                            return false;
                        }""")
                        if has_next:
                            time.sleep(3)
                        else:
                            print("    No next button found", flush=True)
                            break
                    except Exception as e:
                        print("    Pagination error:", e, flush=True)
                        break
                        
            print(f"Total captured for {notice_type}: {len(captured_items)} items", flush=True)
            
            # 处理每条公告
            import urllib.parse
            detail_page = context.new_page()
            try:
                def _route_block(route):
                    try:
                        rtype = route.request.resource_type
                        if rtype in ("image", "media", "font", "stylesheet"):
                            route.abort()
                        else:
                            route.continue_()
                    except Exception:
                        try:
                            route.continue_()
                        except Exception:
                            pass
                
                detail_page.route("**/*", _route_block)
            except Exception:
                pass

            for item in captured_items:
                title = item.get('title') or ''
                url = item.get('href') or ''
                
                if not url.startswith('http'):
                    url = urllib.parse.urljoin("https://ceec.dnezb.com/", url)
                
                print(f"Processing: {title[:60]}...", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                    
                try:
                    detail_page.goto(url, timeout=60000, referer=base_url)
                    try:
                        detail_page.wait_for_load_state('domcontentloaded', timeout=15000)
                    except:
                        pass
                    time.sleep(0.5)
                    
                    # 提取正文文本和HTML
                    content_data = detail_page.evaluate("""() => {
                        let text = "";
                        let html = "";
                        const iframe = document.querySelector('iframe');
                        if (iframe) {
                            try {
                                text = iframe.contentDocument.body.innerText;
                                html = iframe.contentDocument.body.innerHTML;
                                if (text && text.length > 50) return {text, html};
                            } catch(e) {
                                // ignore CORS error
                            }
                        }
                        const selectors = ['.content-box', '.notice-content', '.detail-content', '.ql-editor', '.content', '.article', '.news_content', '#Div2', '.infoCon', '.article_box', '#printArea', '.NoticeDetail'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 50) {
                                return {text: el.innerText, html: el.innerHTML};
                            }
                        }
                        return {text: document.body.innerText, html: document.body.innerHTML};
                    }""")
                    
                    content_text = content_data.get('text', '')
                    content_html = content_data.get('html', '')
                    
                    # 尝试从详情页提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date, .publish-time, .vT_detail_content_content_text_t');
                        if (timeEl) return timeEl.innerText;
                        
                        const allDivs = document.querySelectorAll('div, span, p');
                        for (const div of allDivs) {
                            const text = div.innerText;
                            if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && (text.includes('时间') || text.includes('发布'))) {
                                return text;
                            }
                        }
                        return null;
                    }""")
                    
                    # 正文匹配
                    matched_competitors = [kw for kw in KEYWORDS if kw in content_text]
                    if matched_competitors:
                        print(f"  Keyword matched in content", flush=True)
                        matched_count += 1
                        
                        pub_date = None
                        if pub_date_str:
                            import re
                            date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                            if date_match:
                                try:
                                    pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                                except:
                                    pass
                                    
                        if not pub_date:
                            pub_date = datetime.now()
                        
                        # 保存
                        push_log(f"命中竞对 {matched_competitors}，保存: {title[:50]}", 'success')
                        process_bidding(db, title, content_html, url, pub_date, notice_type=notice_type, source_website="中国能建电子采购平台", matched_competitors=matched_competitors)
                    else:
                        print(f"  No keyword match in content, skipped", flush=True)
                        
                except Exception as e:
                    print(f"  Error processing {url}: {e}", flush=True)
            try:
                detail_page.close()
            except:
                pass
                        
        push_log(f"中国能建电子采购平台抓取完成，共命中竞对 {matched_count} 条", 'success' if matched_count > 0 else 'warning')
        
    except Exception as e:
        push_log(f"中国能建抓取异常: {e}", 'error')
        print(f"CEEC Crawler error: {e}", flush=True)
    finally:
        page.close()



