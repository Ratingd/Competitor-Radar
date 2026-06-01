import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from app.services.crawler_service import push_log
import time

def send_report_email(email_config, excel_bytes, word_bytes, html_content):
    msg = MIMEMultipart()
    msg['From'] = email_config.sender
    msg['To'] = email_config.receiver
    msg['Subject'] = f"竞对雷达 - 抓取分析报告 {time.strftime('%Y-%m-%d')}"

    # 正文
    msg.attach(MIMEText(html_content, 'html'))

    # 附件 - Excel
    if excel_bytes:
        part_excel = MIMEApplication(excel_bytes)
        part_excel.add_header('Content-Disposition', 'attachment', filename="竞对清单.xlsx")
        msg.attach(part_excel)

    # 附件 - Word
    if word_bytes:
        part_word = MIMEApplication(word_bytes)
        part_word.add_header('Content-Disposition', 'attachment', filename="竞对分析报告.docx")
        msg.attach(part_word)

    try:
        if email_config.smtp_port in [465, 994]:
            server = smtplib.SMTP_SSL(email_config.smtp_server, email_config.smtp_port)
        else:
            server = smtplib.SMTP(email_config.smtp_server, email_config.smtp_port)
            server.starttls()
        
        # 针对 139 邮箱等特殊情况，登录用户名可能需要去掉 @ 域名部分
        login_user = email_config.sender
        if '139.com' in email_config.sender:
            login_user = email_config.sender.split('@')[0]
            
        try:
            server.login(login_user, email_config.password)
        except smtplib.SMTPAuthenticationError as e:
            if '139.com' in email_config.sender:
                # 尝试使用全称重试
                server.login(email_config.sender, email_config.password)
            else:
                raise e
                
        server.send_message(msg)
        server.quit()
        push_log("邮件发送成功！", "success")
        return True
    except smtplib.SMTPException as e:
        push_log(f"邮件发送失败 (SMTP错误): {str(e)}", "error")
        return False
    except ConnectionResetError as e:
        push_log(f"邮件发送失败 (连接被重置): {str(e)}\n请检查防火墙、杀毒软件拦截，或稍后重试。", "error")
        return False
    except Exception as e:
        push_log(f"邮件发送失败 (未知错误): {str(e)}", "error")
        return False
