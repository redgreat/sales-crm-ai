"""LLM organization of recognized text; never overwrites the recognition original."""
from __future__ import annotations

import json
import re
from typing import Any

from app.enhanced_input import StageError

_PROMPT = """你是 CRM 销售记录整理助手。仅整理语序、标点和段落，不补充事实。
姓名、客户名称、数字、金额、日期必须保留原样；不确定处保留原词。只输出 JSON：
{{"optimized_text":"整理后的全文"}}
<<OUTPUT_CONTRACT>>enhanced_text<<END_OUTPUT_CONTRACT>>
<<USER_TEXT>>
{raw_text}
<<END_USER_TEXT>>"""
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_MAX_CHARS = 30000


async def organize_text(model: Any, raw_text: str) -> str:
    """Return a suggestion for human review, guarding obvious numeric changes."""
    if not raw_text.strip():
        raise StageError("organize", "EMPTY_RECOGNITION", "识别原文为空")
    if len(raw_text) > _MAX_CHARS:
        raise StageError("organize", "TEXT_TOO_LONG", "识别原文超过整理上限；原文已保留，可分段处理")
    try:
        response = await model.ainvoke(_PROMPT.format(raw_text=raw_text))
        content = response.content if isinstance(response.content, str) else str(response.content)
        payload = json.loads(content.strip().removeprefix("```json").removesuffix("```").strip())
        optimized = payload.get("optimized_text") if isinstance(payload, dict) else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise StageError("organize", "INVALID_MODEL_OUTPUT", "整理输出不是合法 JSON；识别原文仍可查看") from exc
    except Exception as exc:
        raise StageError("organize", "MODEL_UNAVAILABLE", "整理模型暂不可用；识别原文仍可查看") from exc
    if not isinstance(optimized, str) or not optimized.strip():
        raise StageError("organize", "EMPTY_MODEL_OUTPUT", "整理文本为空；识别原文仍可查看")
    if sorted(_NUMBER.findall(raw_text)) != sorted(_NUMBER.findall(optimized)):
        raise StageError("organize", "NUMBER_CHANGED", "整理改变了数字或日期；识别原文仍可查看")
    return optimized.strip()
