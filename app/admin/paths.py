"""配置后台的启用判定。

后台开关：`admin.enabled`（总开关）+ `admin.allow_prod`（生产放行）。
非生产沿用 `api.playground.enabled` 作为联调入口的开关；生产一律默认关闭，
必须显式 `admin.allow_prod=true` 才暴露管理面（建议仅限内网访问）。
后台配置存 PG，不依赖本仓配置文件路径，因此判定只看环境与开关。
"""
from __future__ import annotations

from app.config import Settings


def admin_enabled(settings: Settings) -> bool:
    if not settings.admin.enabled:
        return False
    if settings.environment == "prod":
        return settings.admin.allow_prod
    return settings.api.playground.enabled


def require_admin_enabled(settings: Settings) -> None:
    from app.errors import NotFound

    if not admin_enabled(settings):
        raise NotFound("配置后台未启用：需要 admin.enabled=true 且非生产环境开启 "
                       "api.playground.enabled，或生产环境显式 admin.allow_prod=true")
