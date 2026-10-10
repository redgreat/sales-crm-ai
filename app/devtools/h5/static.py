"""H5 联调页的内置静态资源：单文件页面与挂载前缀注入。

页面来自 `tests/h5/index.html`，由 Dockerfile 复制到镜像 `/app/h5-dist/index.html`；
本地开发（无 h5-dist 目录）直接回落 `tests/h5/index.html`。
"""
from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT

# 查找顺序：镜像内产物优先，本地仓库源文件兜底
_DIST_CANDIDATES = (Path("h5-dist"), Path("tests") / "h5")

# 挂载前缀注入：页面所有 /crm、/__login 等调用都基于 window.__H5_BASE
BASE_SCRIPT = "<script>window.__H5_BASE='/h5';</script>"


def index_file() -> Path | None:
    for candidate in _DIST_CANDIDATES:
        target = PROJECT_ROOT / candidate / "index.html"
        if target.is_file():
            return target
    return None


def render_index(path: Path) -> bytes:
    """读入页面并注入挂载前缀；已注入过则不重复注入。"""
    html = path.read_text(encoding="utf-8")
    if BASE_SCRIPT not in html and "<head>" in html:
        html = html.replace("<head>", "<head>" + BASE_SCRIPT, 1)
    return html.encode("utf-8")
