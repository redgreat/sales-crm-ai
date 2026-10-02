"""图片文字识别适配层（P5 需求 4.13：图片 → 原图锚点 → 抽取）。

实现对象：阿里云文字识别 OCR 的统一识别接口 `RecognizeAllText`
（产品 `ocr-api`，版本 `2021-07-07`，Endpoint 固定华东1 杭州）。

与需求一致的行为约束：

- 图片可用 `Url` 或二进制 `body` 二选一传入（二者互斥、不可同空），
  因此**不强依赖对象存储**——这与 ASR（必须公网 URL）不同。
- 开启 `OutputCoordinate` 后返回坐标，据此装配**原图锚点**，
  供人工核对关键数字/日期（需求 4.13 要求关键数字日期必须确认）。
- 本层只产出文本与锚点，**不做任何“确认/写入”动作**；确认在人工侧。
- 未启用 / 未配置凭据时 `build_ocr_client()` 返回 `None`，调用方须显式报
  “能力未启用”，不回退为假装识别成功。

返回字段以官方 OpenAPI 为准；解析缺失时显式失败，不静默填空。
"""
from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.providers.stub import strip_usage_metadata


class OcrError(RuntimeError):
    """识别失败：凭据/入参不合法、调用失败或结果不可解析。"""


@dataclass(frozen=True)
class OcrRegion:
    """带原图锚点的文本区域。"""

    text: str
    # 多边形顶点（原图坐标）；为空表示未开启坐标返回
    points: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class OcrResult:
    """识别结果：全文 + 带锚点区域。"""

    text: str
    regions: tuple[OcrRegion, ...] = ()

    def anchored_text(self) -> str:
        """渲染带原图锚点的文本（供人工核对关键数字/日期定位到原图位置）。"""
        if not self.regions:
            return self.text
        lines: list[str] = []
        for index, region in enumerate(self.regions, start=1):
            anchor = f"<R{index}>" if not region.points else f"<R{index}@{_fmt_points(region.points)}>"
            lines.append(f"{anchor} {region.text}")
        return "\n".join(lines)


def _fmt_points(points: tuple[tuple[float, float], ...]) -> str:
    return ",".join(f"{x:.0f},{y:.0f}" for x, y in points[:4])


class OcrClient(Protocol):
    async def recognize(
        self, *, image_url: str | None = None, image_bytes: bytes | None = None
    ) -> OcrResult: ...


class AliyunOcrClient:
    """阿里云 OCR 客户端（官方 SDK 处理签名；本层只做入参与结果规整）。"""

    def __init__(self, settings: Settings):
        # 延迟导入：SDK 未安装时给出明确提示，而不是让整个模块无法导入
        try:
            from alibabacloud_ocr_api20210707.client import Client as OcrApiClient
            from alibabacloud_tea_openapi.models import Config as OpenApiConfig
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise OcrError(
                "未安装阿里云 OCR SDK，无法启用 OCR：pip install alibabacloud_ocr_api20210707"
            ) from exc

        self._settings = settings.ocr
        config = OpenApiConfig(
            access_key_id=self._settings.access_key_id,
            access_key_secret=self._settings.access_key_secret,
            endpoint=self._settings.endpoint,
        )
        self._client = OcrApiClient(config)

    async def recognize(
        self, *, image_url: str | None = None, image_bytes: bytes | None = None
    ) -> OcrResult:
        # Url 与 body 互斥且不可同空（OpenAPI 约束）
        if bool(image_url) == bool(image_bytes):
            raise OcrError("image_url 与 image_bytes 必须二选一（不可同时提供或同时为空）")

        from alibabacloud_ocr_api20210707.models import RecognizeAllTextRequest

        kwargs: dict[str, Any] = {
            "type": self._settings.type,
            "output_coordinate": self._settings.output_coordinate,
        }
        if image_url:
            kwargs["url"] = image_url
        else:
            # 二进制直传（不是 base64）：官方文档标注为 string<binary>，≤10MB
            kwargs["body"] = _to_stream(image_bytes)

        request = RecognizeAllTextRequest(**kwargs)
        # 用同步方法 + to_thread：SDK 的 *_async 链路在 tea-openapi 0.4.6 组合下
        # 处理二进制 body 时抛 "object bytes can't be used in 'await'"（2026-10-02 POC 实测），
        # 同步路径成熟稳定，线程外移避免阻塞事件循环。
        response = await asyncio.to_thread(
            self._client.recognize_all_text_with_options, request, _runtime()
        )
        body = getattr(response, "body", None)
        data = strip_usage_metadata(_tea_to_map(getattr(body, "data", None)))
        if not isinstance(data, dict) or not data:
            raise OcrError("OCR 未返回识别结果（data 为空）")
        return _parse(data)


def _tea_to_map(value: Any) -> Any:
    """Tea SDK 模型 → dict（模型只有 to_map()，属性访问不产出 dict）。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    to_map = getattr(value, "to_map", None)
    return to_map() if callable(to_map) else value


def _to_stream(image_bytes: bytes | None) -> Any:
    """二进制 → 文件流（SDK 的 body 参数需要可读取的流对象）。"""
    from io import BytesIO

    return BytesIO(image_bytes or b"")


def _runtime() -> Any:
    # RuntimeOptions 在 tea-util 包（alibabacloud_darabonba_runtime 并不存在——
    # 2026-10-02 POC 实测修正）
    from alibabacloud_tea_util.models import RuntimeOptions

    return RuntimeOptions()


def _parse(data: dict[str, Any]) -> OcrResult:
    text = str(data.get("content") or data.get("Content") or data.get("text") or "").strip()
    regions = _extract_regions(data)
    if not text and not regions:
        raise OcrError("OCR 结果既无全文也无文本区域（无法形成可用结果）")
    if not text:
        text = "\n".join(region.text for region in regions)
    return OcrResult(text=text, regions=regions)


def _extract_regions(data: dict[str, Any]) -> tuple[OcrRegion, ...]:
    """提取带坐标区域。

    RecognizeAllText 高精版真实返回（2026-10-02 POC 实测）：
      sub_images[].block_info.block_details[].block_content / block_points([{"X":..,"Y":..}])
    其他键保留宽容匹配，缺失坐标时仍保留文本。
    """
    regions: list[OcrRegion] = []
    for sub in data.get("sub_images") or data.get("SubImages") or []:
        if not isinstance(sub, dict):
            continue
        block_info = sub.get("block_info") or sub.get("BlockInfo") or {}
        details = block_info.get("block_details") or block_info.get("BlockDetails") or []
        for item in details:
            if not isinstance(item, dict):
                continue
            region_text = str(
                item.get("block_content") or item.get("BlockContent")
                or item.get("text") or item.get("content") or ""
            ).strip()
            if not region_text:
                continue
            points_raw = item.get("block_points") or item.get("BlockPoints") or []
            regions.append(OcrRegion(text=region_text, points=_extract_points({"points": points_raw})))
        if regions:
            break
    if not regions:
        for key in ("regions", "lines", "words", "blocks"):
            items = data.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                region_text = str(item.get("text") or item.get("content") or "").strip()
                if not region_text:
                    continue
                regions.append(OcrRegion(text=region_text, points=_extract_points(item)))
            if regions:
                break
    return tuple(regions)


def _extract_points(item: dict[str, Any]) -> tuple[tuple[float, float], ...]:
    raw = item.get("points") or item.get("position") or item.get("box")
    if isinstance(raw, list) and raw:
        # 形式一：[{"x":..,"y":..}, ...]
        if isinstance(raw[0], dict):
            points = [
                (float(p.get("x", p.get("X", 0))), float(p.get("y", p.get("Y", 0))))
                for p in raw
                if isinstance(p, dict)
            ]
            return tuple(points)
        # 形式二：[x1,y1,x2,y2,...] 扁平坐标
        if isinstance(raw[0], (int, float)):
            flat = [float(v) for v in raw]
            return tuple((flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2))
    return ()


class StubOcrClient:
    """联调 Stub：返回固定文本与锚点，不访问任何外部服务。"""

    def __init__(self, result: OcrResult | None = None):
        self._result = result or OcrResult(
            text="续约价格政策：满 50 万减 2 万。",
            regions=(OcrRegion(text="续约价格政策：满 50 万减 2 万。", points=((10, 20), (300, 20), (300, 60), (10, 60))),),
        )

    async def recognize(
        self, *, image_url: str | None = None, image_bytes: bytes | None = None
    ) -> OcrResult:
        return OcrResult(text=self._result.text, regions=self._result.regions)


def build_ocr_client(settings: Settings) -> OcrClient | None:
    """未启用返回 None——调用方须显式报“能力未启用”，不得静默兜底。"""
    if not settings.ocr.enabled:
        return None
    return AliyunOcrClient(settings)
