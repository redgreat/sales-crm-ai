"""对象存储适配层：为 ASR 提供公网可访问的音频 URL（需求 4.12 的前置依赖）。

为什么需要它：百炼录音文件识别**只接受公网可访问的 URL**，明确不支持
Base64、二进制流或本地文件。因此：

- 启用 OSS：本地录音可「上传 → 取签名 URL → 提交识别」；
- 未启用 OSS：ASR 只能识别调用方**自行提供**的公网 URL（例如 CRM 已有的
  文件服务器地址），此时上传本地文件必须显式报错，不能假装可用。

`oss2` 为同步 SDK，这里用 `asyncio.to_thread` 包装，避免阻塞事件循环。
签名 URL 带时效（默认 1 小时），足以覆盖 ASR 任务提交；不依赖 bucket 公共读。
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from app.config import Settings


class OssError(RuntimeError):
    """上传失败或对象存储未正确配置。"""


class OssClient(Protocol):
    async def upload(
        self, *, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """上传对象并返回公网可访问的 URL。"""
        ...


class AliyunOssClient:
    """阿里云 OSS 客户端：上传并返回带签名的公网 URL。"""

    def __init__(self, settings: Settings):
        try:
            import oss2
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise OssError("未安装 oss2，无法启用对象存储：pip install oss2") from exc

        self._settings = settings.oss
        self._oss2 = oss2
        self._bucket = None  # 惰性构造，见 _get_bucket()

    def _get_bucket(self):
        """惰性构造 bucket。

        为什么不放进 `__init__`：oss2 在构造 `Bucket` 时即校验 endpoint/bucket 名，
        配置稍有偏差就会抛出 oss2 原生异常，绕过本层统一的 `OssError` 语义，
        且会让 `build_oss_client()` 在进程启动阶段就崩。惰性构造保证：
        构建客户端永不因配置形状失败，错误一律在真正上传处、以 `OssError` 暴露。
        """
        if self._bucket is None:
            cfg = self._settings
            auth = self._oss2.Auth(cfg.access_key_id, cfg.access_key_secret)
            self._bucket = self._oss2.Bucket(auth, cfg.endpoint, cfg.bucket)
        return self._bucket

    async def upload(
        self, *, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        if not key.strip():
            raise OssError("object key 不能为空")

        bucket = self._get_bucket()

        def _put() -> str:
            bucket.put_object(key, data, headers={"Content-Type": content_type})
            return bucket.sign_url("GET", key, self._settings.signed_url_ttl_seconds)

        try:
            return await asyncio.to_thread(_put)
        except OssError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 统一转为 OssError
            raise OssError(f"对象上传失败: {exc}") from exc


class StubOssClient:
    """联调 Stub：不真正上传，返回可辨识的占位 URL。"""

    def __init__(self, prefix: str = "stub://oss"):
        self._prefix = prefix

    async def upload(
        self, *, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        return f"{self._prefix}/{key}"


def build_oss_client(settings: Settings) -> OssClient | None:
    """未启用返回 None——调用方须显式报“未启用”，不得静默兜底。"""
    if not settings.oss.enabled:
        return None
    return AliyunOssClient(settings)
