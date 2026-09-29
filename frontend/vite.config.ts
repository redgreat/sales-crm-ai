import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import process from 'node:process';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) => filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			}
		})
	],
	server: {
		proxy: {
			// 开发环境把联调代理转发到 AI API（生产同源部署或经 CRM 网关）
			'/playground': {
				target: process.env.AI_API_URL ?? 'http://127.0.0.1:8310',
				changeOrigin: true
			}
		}
	}
});
