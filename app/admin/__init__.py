"""配置后台（管理端）：连接与账号全部落 PG。

- `admin_connections` —— 模型服务 / 外部接口 / MCP 连接及随连接的凭据
- `admin_users`      —— 管理端账号（PBKDF2 哈希 + 角色权限），与 CRM 登录无关

红线：凭据只写不回显（响应里只有「是否已配置」）、不进日志；
后台保存后立即重算生效配置，无需重启。
"""
from __future__ import annotations

from app.admin import effective, overview, paths, repo, runtime, schema, secrets, session, users

__all__ = ["effective", "overview", "paths", "repo", "runtime", "schema", "secrets", "session", "users"]
