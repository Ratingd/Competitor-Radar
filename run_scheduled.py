import os
import sys
import json
from datetime import datetime

# 强制 Playwright 浏览器读取路径为用户 AppData 目录，避免权限和路径丢失问题
browsers_path = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "AppData", "Local", "ms-playwright-business-radar")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

# 切换到脚本所在目录（即backend目录）
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.getcwd())

# 确保能被正常导入并且输出日志
log_file = "schedule_error.log"
def log(msg):
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

try:
    from app.models.database import SessionLocal
    from app.services.crawler_service import run_crawler_task_for_websites
    from main import EmailConfig
except Exception as e:
    import traceback
    log(f"Import Error:\n{traceback.format_exc()}")
    sys.exit(1)

def main():
    log("Started scheduled task")
    config_path = "schedule_config.json"
    if not os.path.exists(config_path):
        log("config file not found")
        return
        
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            
        if not config.get("enable"):
            log("task is disabled in config")
            return
            
        db = SessionLocal()
        try:
            # 重建 EmailConfig 对象
            email_conf_dict = config.get("email_config")
            email_conf = None
            if email_conf_dict:
                email_conf = EmailConfig(**email_conf_dict)
            
            # 开始爬取
            log("Starting crawler task...")
            run_crawler_task_for_websites(
                db=db,
                websites=config.get("websites", []),
                keywords=config.get("keywords", []),
                email_config=email_conf
            )
            log("Crawler task finished successfully.")
        finally:
            db.close()
            
    except Exception as e:
        # 如果后台执行出错，可以记录到日志文件排查
        import traceback
        log(f"Execution Error:\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()
