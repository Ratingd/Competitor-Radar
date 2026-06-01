import os
import sys
import asyncio
from datetime import datetime

# 强制 Playwright 浏览器读取路径为用户 AppData 目录或当前工作区目录
browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright"))
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

# 切换到脚本所在目录（即backend目录）
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.getcwd())

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

try:
    from app.models.database import SessionLocal, engine, Base
    from app.services.crawler_service import run_crawler_task_for_websites
except Exception as e:
    import traceback
    log(f"Import Error:\n{traceback.format_exc()}")
    sys.exit(1)

def main():
    log("Started GitHub Actions scheduled task")
    
    # 确保数据库表存在
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        log(f"Database init error: {e}")

    feishu_webhook_url = os.environ.get("FEISHU_WEBHOOK_URL")
    if not feishu_webhook_url:
        log("Warning: FEISHU_WEBHOOK_URL is not set in environment variables.")
        
    # 竞对公司列表
    competitors_env = os.environ.get("COMPETITORS")
    if competitors_env:
        keywords = [k.strip() for k in competitors_env.split(",") if k.strip()]
    else:
        keywords = [
            "广东省电信规划设计院有限公司",
            "广东南方电信规划咨询设计院有限公司",
            "中通服中睿科技有限公司",
            "中讯邮电咨询设计院有限公司广东分公司",
            "华信咨询设计研究院有限公司",
            "吉林吉大通信设计院股份有限公司",
            "公诚管理咨询有限公司",
            "广州瀚信通信科技股份有限公司",
            "宜通世纪科技股份有限公司",
            "广东原创科技有限公司"
        ]

    # 目标网站（默认爬取所有）
    websites = ['guangdong', 'guangxi', 'cmcc', 'chinatelecom', 'shenzhen', 'guangzhou', 'unicom', 'gdzy', 'hainan', 'chinatower', 'gdzjcs', 'zycg', 'ccgp', 'dfmc', 'travelsky', 'powerchina', 'ceec']
    
    db = SessionLocal()
    try:
        log("Starting crawler task...")
        log(f"Keywords (Competitors): {keywords}")
        run_crawler_task_for_websites(
            db=db,
            websites=websites,
            keywords=keywords,
            email_config=None,  # GitHub Actions 默认使用飞书，不使用邮件
            feishu_webhook_url=feishu_webhook_url
        )
        log("Crawler task finished successfully.")
    except Exception as e:
        import traceback
        log(f"Execution Error:\n{traceback.format_exc()}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    main()
