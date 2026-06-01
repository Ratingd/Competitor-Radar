import requests
import json
import logging

logger = logging.getLogger(__name__)

def send_to_feishu(webhook_url: str, biddings: list):
    """
    Send the scraped bidding data to a Feishu group bot via webhook.
    """
    if not webhook_url:
        logger.warning("Feishu webhook URL is not provided. Skipping Feishu notification.")
        return False

    if not biddings:
        # If no biddings, we can optionally send a 'no update' message.
        text_content = "今日竞对雷达自动抓取完毕，暂无发现新的竞对动态。"
        msg_payload = {
            "msg_type": "text",
            "content": {
                "text": text_content
            }
        }
    else:
        # Construct an interactive message (card) or rich text for biddings.
        # Given Feishu limits, we shouldn't send too much. Let's send a card summarizing the findings.
        elements = []
        for i, b in enumerate(biddings[:10], 1):  # Limit to 10 to avoid too large payload
            elements.append({
                "tag": "div",
                "text": {
                    "content": f"**{i}. {b.title}**\n来源: {b.source_website} | 类型: {b.notice_type}\n发布日期: {b.publish_date.strftime('%Y-%m-%d') if b.publish_date else '未知'}\n[查看原文]({b.source_url})",
                    "tag": "lark_md"
                }
            })
            elements.append({
                "tag": "hr"
            })
            
        if len(biddings) > 10:
            elements.append({
                "tag": "div",
                "text": {
                    "content": f"*(由于篇幅限制，还有 {len(biddings) - 10} 条动态未展示)*",
                    "tag": "lark_md"
                }
            })

        msg_payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🚨 竞对雷达: 发现 {len(biddings)} 条新动态"
                    },
                    "template": "red"
                },
                "elements": elements
            }
        }

    try:
        response = requests.post(
            webhook_url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(msg_payload),
            timeout=10
        )
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("code") == 0:
                logger.info("Successfully sent message to Feishu bot.")
                return True
            else:
                logger.error(f"Feishu bot returned error: {res_json}")
        else:
            logger.error(f"Failed to send to Feishu, status code: {response.status_code}, response: {response.text}")
    except Exception as e:
        logger.error(f"Exception while sending to Feishu: {e}")
    
    return False
