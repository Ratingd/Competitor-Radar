from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # 智谱 AI (GLM-4-Flash) 配置
    ZHIPU_API_KEY: str = "606fb529f5d14066ba8e8cd39232a6d4.ljYOj0cXRbfNifoZ"
    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    AI_MODEL_NAME: str = "glm-4-flash"
    
    # 保留旧配置兼容
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    
    DATABASE_URL: str = "sqlite:///./sql_app.db"

    class Config:
        env_file = ".env"

settings = Settings()
