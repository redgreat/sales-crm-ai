import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import process from 'node:process';

export default defineConfig({
	plugins: [
		tailwindcss(),
		// 后台挂载在 /admin 下，开发时访问根路径直接跳过去，避免 404 困惑
		{
			name: 'admin-base-redirect',
			configureServer(server) {
				server.middlewares.use((req, res, next) => {
					if (req.url === '/' || req.url === '' || req.url === '/admin') {
						res.writeHead(302, { Location: '/admin/' });
						res.end();
						return;
					}
					next();
				});
			}
		},
		// 不传参：传了会让 SvelteKit 忽略 svelte.config.js（adapter / paths 失效）
		sveltekit()
	],
	server: {
		proxy: {
			// 后台接口同源直连（生产由 FastAPI 自身提供，开发转发到 AI 服务）
			'/api': {
				target: process.env.AI_API_URL ?? 'http://127.0.0.1:8310',
				changeOrigin: true
			},
			// 健康检查 + H5 联调代理
			'/health': {
				target: process.env.AI_API_URL ?? 'http://127.0.0.1:8310',
				changeOrigin: true
			},
			'/playground': {
				target: process.env.AI_API_URL ?? 'http://127.0.0.1:8310',
				changeOrigin: true
			}
		}
	}
});
