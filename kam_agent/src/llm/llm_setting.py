import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain_deepseek import ChatDeepSeek


load_dotenv()


@lru_cache
def get_llm() -> ChatDeepSeek:
    """创建并缓存全项目共用的 DeepSeek 客户端。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError("未读取到 DEEPSEEK_API_KEY，请检查 .env 文件")

    return ChatDeepSeek(
        model="deepseek-chat",
        api_key=api_key,
        temperature=0,
        max_retries=2,
    )