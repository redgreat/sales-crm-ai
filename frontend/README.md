# AI 配置后台

开发/测试环境独立入口：`http://127.0.0.1:5174/`。页面管理模型、外部接口和企业查询 MCP 的非密钥参数；请求经同源 `/playground/api` 代理到 AI 服务。生产环境不开放此管理接口。

密钥不进入浏览器，仍在 `conf/config.yml` 或部署环境变量中注入。页面保存的设置位于忽略文件 `conf/config.ui.yml`，重启 AI 服务后生效。若使用仓库外的 `SAI_CONFIG`，配置后台拒绝写入。

```bash
npm ci
npm run dev -- --host 127.0.0.1 --port 5174
npm run check
```
