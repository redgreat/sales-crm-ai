"""H5 测试联调页（`/h5`）：静态页 + 服务端登录会话 + CRM 同源代理。

定位：**仅测试联调用**，与正常业务代码隔离。整个功能集中在本目录：
- `static.py`：内置静态页与挂载前缀注入
- `session.py`：进程内登录会话（token 只存内存，不外泄）
- `config.py`：CRM 地址与 OAuth 参数解析（读配置后台的 CRM 连接）
- `routes.py`：`/h5`、`/h5/__login|__logout|__session`、`/h5/crm/**`

行为：与 `scripts/h5_server.py`（本地开发用）同一套语义，只是随镜像内置、由 AI 服务
同端口提供。开关与 `/playground` 一致：仅 dev/test 且 `api.playground.enabled`，prod 一律 404。

清理方式（一条命令 + 三处小改动）：
1. 删除本目录 `app/devtools/`（连同 `app/devtools/__init__.py`）；
2. `app/api/main.py`：删掉 `from app.devtools.h5 import router as h5_router` 与 `app.include_router(h5_router)` 两行；
3. `Dockerfile` 删掉 `COPY tests/h5/index.html ./h5-dist/index.html`，`.dockerignore` 删掉两条 `!tests/h5/...`；
4. （可选）`tests/test_h5_page.py`、`tests/h5/index.html` 一并删除；
   `app/admin/schema.py`、`app/api/routes/settings.py`、前端 CRM 表单里的
   `token_endpoint/client_id/scopes/client_secret` 只是给本页配登录参数，保留不影响其他功能。
"""
from __future__ import annotations

from app.devtools.h5.routes import router

__all__ = ["router"]