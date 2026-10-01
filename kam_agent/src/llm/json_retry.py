"""只在模型回复无法解析为 JSON 时，额外请求模型一次。"""

import logging
import os
import sys
from collections.abc import Callable

from src.llm.llm_setting import get_llm


logger = logging.getLogger(__name__)


def invoke_and_parse_json(
    messages: list,
    parse_json: Callable[[str], dict],
    model=None,
    retries: int = 1,
    on_response: Callable[[str], None] | None = None,
) -> dict:
    """调用模型并解析回复；仅解析异常触发重试，调用异常直接向上传递。"""
    for attempt in range(retries + 1):
        response = (model or get_llm()).invoke(messages)
        if os.environ.get("KAM_DEBUG_LLM_RAW") == "1":
            print(f"[LLM JSON attempt {attempt + 1}]\n{response.content}", file=sys.stderr)
        if on_response is not None:
            on_response(response.content)
        try:
            return parse_json(response.content)
        except ValueError:
            if attempt == retries:
                raise
            logger.warning("LLM JSON parse failed; retrying once")
