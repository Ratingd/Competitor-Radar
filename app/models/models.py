from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, JSON, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.models.database import Base

class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    openid = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, nullable=True)
    custom_keywords = Column(JSON, default=list) # 存储列表
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Bidding(Base):
    __tablename__ = "biddings"

    bid_id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    source_url = Column(String, unique=True, index=True)
    publish_date = Column(DateTime) # 发布时间
    content_abstract = Column(Text) # 摘要
    category = Column(String, index=True) # 业务分类
    notice_type = Column(String, index=True, default="中标公告") # 公告类型：中标公告/结果公告
    source_website = Column(String, default="广东省政府采购网") # 来源网站
    ai_score = Column(Float) # 相关性评分
    raw_html = Column(Text) # 原始 HTML (或者纯文本正文)
    meta_info = Column(JSON, default={}) # 存储额外的 AI 提取信息 (预算、截止时间等)
    opportunity_analysis = Column(Text, default="") # 竞对分析
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class PushLog(Base):
    __tablename__ = "push_logs"

    log_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"))
    bid_id = Column(Integer, ForeignKey("biddings.bid_id"))
    push_status = Column(String) # email, wechat, both, failed
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")
    bidding = relationship("Bidding")
