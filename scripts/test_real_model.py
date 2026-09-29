"""真实 LLM 模型层直测：不经数据库，直接验证 Provider 与 prompt 契约。

用法：python scripts/test_real_model.py [用户文本]
默认跑两条：信息完整（应产出任务候选）与缺日期（应给出 missing）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re

from app.config import get_settings
from app.graphs.nodes import PROMPT_TEMPLATE, _JSON_RE, _missing_fields
from app.providers.factory import build_chat_model

FULL_TEXT = (
    "客户：ACME公司\n"
    "今天上午拜访了采购负责人王经理，聊了三季度采购计划和交付节奏，对方希望下周给报价。\n"
    "任务：电话回访王经理\n"
    "负责人：张三\n"
    "2026-10-08"
)

MISSING_TEXT = (
    "客户：华信集团\n"
    "电话里李总提到续约意向，需要上门详细谈一次方案。\n"
    "任务：拜访客户总监李总"
)


async def probe(model, user_text: str, label: str) -> dict:
    prompt = PROMPT_TEMPLATE.format(user_text=user_text)
    started = time.monotonic()
    response = await model.ainvoke(prompt)
    elapsed = time.monotonic() - started
    content = response.content if isinstance(response.content, str) else str(response.content)
    match = _JSON_RE.search(content)
    if not match:
        print(f"[{label}] FAIL {elapsed:.1f}s：输出不是 JSON →\n{content[:400]}")
        return {}
    payload = json.loads(match.group(0))
    missing = _missing_fields(payload)
    print(
        f"[{label}] {elapsed:.1f}s 客户={payload.get('customers')} "
        f"任务数={len(payload.get('tasks', []))} 缺参={[(m['field']) for m in missing]}"
    )
    for task in payload.get("tasks", []):
        print(f"    任务: {json.dumps(task, ensure_ascii=False)}")
    usage = getattr(response, "usage_metadata", None)
    if usage:
        print(f"    （供应商 usage 仅存在于本次响应对象，已确认不会进任何持久化：{usage.get('output_tokens')} tokens out）")
    return payload


async def main() -> int:
    settings = get_settings()
    if settings.model.provider != "openai_compatible":
        print("conf/config.yml 的 model.provider 不是 openai_compatible")
        return 2
    print(f"Provider: {settings.model.name} @ {settings.model.base_url}")
    model = build_chat_model(settings)

    texts = [sys.argv[1]] if len(sys.argv) > 1 else [FULL_TEXT, MISSING_TEXT]
    for index, text in enumerate(texts, 1):
        await probe(model, text, f"用例{index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
