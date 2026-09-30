"""普通文件解析适配层（P5 需求 4.14：非录音、非图片的普通文件 → 文本 → 抽取）。

与 ASR/OCR 不同，本层**不依赖任何外部服务与凭据**，可独立完成。

边界（刻意收窄，不做"什么都能解析"的承诺）：

- 只处理**纯文本类**文件：txt / md / log / csv / json / yaml。
- docx、pdf、xlsx 等二进制或复合文档**明确报错**，不留 stub 假结果——
  它们需要额外依赖与单独的解析质量验证，属于后续 POC，现在不能宣称支持。
- 编码按 utf-8 → utf-8-sig → gb18030 依次尝试；仍失败则显式报错，
  绝不静默替换乱码字符后当解析成功。
- 超过体积上限按字符截断并标记 `truncated`，让调用方能如实提示"内容已截断"，
  而不是把半截内容当成完整材料送进模型。

解析产物只作为**抽取/问答的输入文本**，本层不做任何写入、不产出候选。
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

# 支持的纯文本扩展名（小写）
SUPPORTED_TYPES: frozenset[str] = frozenset(
    {".txt", ".md", ".markdown", ".log", ".csv", ".json", ".yaml", ".yml"}
)
# 编码尝试顺序：中文 Windows 环境常见 gb18030，放在 utf-8 之后
_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "gb18030")
# 默认体积上限（1MB 字符数）；超出即截断，不无限读入
DEFAULT_MAX_BYTES = 1_048_576


class FileError(RuntimeError):
    """文件不可解析：类型不支持、疑似二进制、编码无法识别或内容为空。"""


@dataclass(frozen=True)
class ParsedFile:
    """解析结果：纯文本 + 来源信息（供抽取与人工核对定位来源）。"""

    text: str
    file_type: str
    source_name: str
    size_bytes: int
    encoding: str
    truncated: bool = False

    def summary(self) -> str:
        flag = "（已截断）" if self.truncated else ""
        return f"[{self.source_name} {self.file_type} {self.size_bytes}B{flag}]"


def detect_type(*, name: str = "", content_type: str = "") -> str:
    """推断扩展名（带点、小写）。无法判断时返回空串，由调用方显式报错。"""
    candidate = (name or "").strip().lower()
    if not candidate and content_type:
        # 无文件名时退回 MIME 的简单映射，避免"按扩展名猜不出来就算了"
        mapping = {
            "text/plain": ".txt",
            "text/markdown": ".md",
            "text/csv": ".csv",
            "application/json": ".json",
            "application/x-yaml": ".yaml",
            "text/yaml": ".yaml",
        }
        candidate = mapping.get(content_type.split(";")[0].strip().lower(), "")
    if "." in candidate:
        suffix = "." + candidate.rsplit(".", 1)[-1]
        return suffix if suffix in SUPPORTED_TYPES else ""
    return ""


def _decode(data: bytes) -> tuple[str, str]:
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise FileError("文件编码无法识别（已尝试 utf-8 / utf-8-sig / gb18030）")


def _render_csv(text: str) -> str:
    """CSV → 行内以 ` | ` 分隔的可读文本（保留表头语义，避免纯逗号噪声）。"""
    reader = csv.reader(io.StringIO(text))
    rows = [" | ".join(cell.strip() for cell in row if cell.strip()) for row in reader]
    return "\n".join(row for row in rows if row)


def _render_json(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FileError(f"JSON 解析失败: {exc}") from exc
    return json.dumps(payload, ensure_ascii=False, indent=None, separators=(", ", ": "))


def parse_bytes(
    *,
    data: bytes,
    name: str = "",
    content_type: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ParsedFile:
    """解析文件字节 → 文本。任一环节不合规都显式抛 FileError。"""
    if not data:
        raise FileError("文件内容为空")

    file_type = detect_type(name=name, content_type=content_type)
    if not file_type:
        raise FileError(
            "不支持的文件类型"
            f"（仅支持 {', '.join(sorted(SUPPORTED_TYPES))}；docx/pdf/xlsx 等需单独 POC，暂不支持）"
        )
    if b"\x00" in data[:4096]:
        raise FileError("疑似二进制文件，不能按纯文本解析")

    raw, encoding = _decode(data)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]

    if file_type == ".csv":
        text = _render_csv(raw)
    elif file_type == ".json":
        text = _render_json(raw)
    else:
        text = raw

    text = _normalize(text)
    if not text.strip():
        raise FileError("文件解析后没有可用文本")

    return ParsedFile(
        text=text,
        file_type=file_type,
        source_name=name or "(未命名)",
        size_bytes=len(data),
        encoding=encoding,
        truncated=truncated,
    )


def _normalize(text: str) -> str:
    """收敛空白：压缩 3 行以上空行、去掉行尾空格（减少无意义 token）。"""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_many(
    files: list[dict[str, Any]], *, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[list[ParsedFile], list[dict[str, str]]]:
    """批量解析：返回 (成功列表, 失败列表)。

    刻意**不因单个文件失败而整批失败**——调用方需要知道哪个文件没解析成，
    而不是拿到一个笼统的错误（M-07 的"逐项列失败原因"是同一思路）。
    """
    parsed: list[ParsedFile] = []
    failed: list[dict[str, str]] = []
    for item in files:
        name = str((item or {}).get("name") or "").strip()
        data = (item or {}).get("data")
        content_type = str((item or {}).get("content_type") or "")
        try:
            if not isinstance(data, (bytes, bytearray)):
                raise FileError("缺少文件字节内容")
            parsed.append(
                parse_bytes(
                    data=bytes(data), name=name, content_type=content_type, max_bytes=max_bytes
                )
            )
        except FileError as exc:
            failed.append({"name": name or "(未命名)", "reason": str(exc)})
    return parsed, failed
