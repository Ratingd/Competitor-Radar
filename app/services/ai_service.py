from openai import OpenAI
from app.core.config import settings
import json

client = OpenAI(
    api_key=settings.ZHIPU_API_KEY,
    base_url=settings.ZHIPU_BASE_URL,
)

def analyze_bidding(title: str, content: str, matched_competitors: list = None) -> dict:
    """
    使用智谱 AI (GLM-4-Flash) 分析中标公告内容，进行竞对分析
    """
    competitors_str = "、".join(matched_competitors) if matched_competitors else "未知"
    prompt = f"""请分析以下中标/成交公告信息，进行竞对（竞争对手）分析。

【公告信息】
标题: {title}
命中匹配的竞对名称: {competitors_str}
内容摘要: {content[:10000]}... (已截断)

【分析要求】
请基于公告内容，分析本次中标项目，提取关键信息并评估竞对情况，返回 JSON 格式结果：

{{
    "category": "业务分类 (如: 智算/算力网络/核心网/承载网/数据中心/云计算/5G/信息化/系统集成/咨询规划/其他)",
    "budget": "中标金额/预算金额 (从文中提取具体金额，如 '5,460,000.00元'，没有则填 '未知')",
    "deadline": "项目工期/服务期 (从文中提取日期或时长，如 '180天' 或 '未知')",
    "supplier": "供应商名称 (直接填入匹配上的竞对名称：{competitors_str})",
    "qualifications": "中标供应商情况 (中标供应商全称，以及其资质/实力等关键信息)",
    "summary": "项目简报 (一句话概括项目内容、金额及中标方)",
    "opportunity_analysis": "竞对分析 (分析本次中标的竞对公司【{competitors_str}】为何能中标，其可能提供的服务方案或核心优势，以及该中标动态所反映出的行业趋势和防范建议)"
}}

【输出要求】
1. 仅返回 JSON 对象，不要包含 markdown 代码块标记
2. opportunity_analysis 要具体，重点分析竞对表现和项目特点"""

    try:
        response = client.chat.completions.create(
            model=settings.AI_MODEL_NAME, 
            messages=[
                {"role": "system", "content": "你是资深的竞对分析专家。请精准分析中标公告中的竞对动态，提取关键信息并进行深度评估，输出结构化的 JSON 数据。只返回 JSON，不要其他文字。"},
                {"role": "user", "content": prompt},
            ],
            stream=False
        )
        result_content = response.choices[0].message.content
        
        # 清理可能存在的 markdown 代码块标记
        result_content = result_content.replace("```json", "").replace("```", "").strip()
        
        # 尝试提取 JSON 部分
        import re
        json_match = re.search(r'\{.*\}', result_content, re.DOTALL)
        if json_match:
            result_content = json_match.group(0)
        
        result = json.loads(result_content)
        
        # 确保所有必要字段存在
        required_fields = ["category", "budget", "deadline", "supplier", "qualifications", "summary", "opportunity_analysis"]
        for field in required_fields:
            if field not in result:
                if field == "supplier":
                    result[field] = competitors_str
                else:
                    result[field] = "未知"
        
        return result
        
    except Exception as e:
        print(f"AI Analysis Error: {e}")
        return {
            "category": "Error",
            "summary": "AI 分析失败",
            "budget": "未知",
            "deadline": "未知",
            "supplier": competitors_str,
            "qualifications": "未知",
            "opportunity_analysis": "分析失败，请人工查看"
        }
