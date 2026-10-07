"""普通文件解析端点（P5 需求 4.14）。

只做一件事：把传入的纯文本文件解析成抽取可用的文本，**不落库、不建候选、
不调用模型**。调用方拿到文本后再自行发起 Run。

入参用 JSON + base64（而非 multipart）：`python-multipart` 缺失会让整个
FastAPI 应用在构建路由阶段直接崩掉，为一个纯文本解析端点引入这种硬依赖
不划算；且本项目其余接口一律 JSON，保持一致。

刻意不因单个文件失败而整批失败——逐项返回失败原因（与 M-07 同思路），
让界面能指出"哪个文件没解析成、为什么"。
"""
from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.errors import ValidationFailed
from app.integrations.files import DEFAULT_MAX_BYTES, FileError, parse_bytes

router = APIRouter(prefix="/api/v1/files", tags=["files"])

# 单次请求的总字节上限，避免把服务当文件服务器用
REQUEST_MAX_BYTES = 5 * 1024 * 1024


class FileItem(BaseModel):
    name: str = ""
    content_type: str = ""
    # UTF-8 base64 编码的文件内容
    data_base64: str = Field(min_length=1)


class ParseFilesBody(BaseModel):
    files: list[FileItem] = Field(default_factory=list)


@router.post("/parse")
async def parse_files(body: ParseFilesBody) -> dict[str, Any]:
    """批量解析文件；返回成功文本与逐项失败原因。"""
    if not body.files:
        raise ValidationFailed("没有传入任何文件", details={"field": "files"})

    parsed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    total = 0
    for item in body.files:
        name = item.name.strip() or "(未命名)"
        remaining = REQUEST_MAX_BYTES - total
        if len(item.data_base64) > 4 * ((remaining + 2) // 3):
            failed.append({"name": name, "reason": "单次请求文件总大小超限（5MB）"})
            continue
        try:
            data = base64.b64decode(item.data_base64, validate=True)
        except (binascii.Error, ValueError):
            failed.append({"name": name, "reason": "data_base64 不是合法的 base64"})
            continue
        if total + len(data) > REQUEST_MAX_BYTES:
            failed.append({"name": name, "reason": "单次请求文件总大小超限（5MB）"})
            continue
        total += len(data)
        try:
            result = parse_bytes(
                data=data,
                name=item.name,
                content_type=item.content_type,
                max_bytes=DEFAULT_MAX_BYTES,
            )
        except FileError as exc:
            failed.append({"name": name, "reason": str(exc)})
            continue
        parsed.append(
            {
                "name": result.source_name,
                "file_type": result.file_type,
                "size_bytes": result.size_bytes,
                "encoding": result.encoding,
                "truncated": result.truncated,
                "text": result.text,
                "summary": result.summary(),
            }
        )

    return {
        "parsed": parsed,
        "failed": failed,
        "count": len(parsed),
        "notes": (
            "仅支持 txt/md/log/csv/json/yaml；docx/pdf/xlsx 需单独 POC，暂不支持。"
            "本端点不落库、不调用模型，解析文本由调用方自行发起 Run。"
        ),
    }
