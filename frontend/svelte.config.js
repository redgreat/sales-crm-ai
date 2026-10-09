import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/**
 * 配置后台以静态 SPA 形式产出（frontend/build），随 Docker 镜像一起发布，
 * 由 FastAPI 挂在 /admin 提供——不需要 Node 运行时。
 *
 * 注意：SvelteKit 在 vite.config.ts 传入 sveltekit(options) 时会忽略本文件，
 * 因此 compilerOptions 也写在这里，vite.config.ts 里的 sveltekit() 保持无参。
 * `paths.base` 必须与实际挂载前缀一致，否则资源 404。
 */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: {
		// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
		runes: ({ filename }) =>
			filename.split(/[/\\]/).includes('node_modules') ? undefined : true
	},
	kit: {
		adapter: adapter({ fallback: 'index.html', strict: false }),
		paths: { base: '/admin' }
	}
};

export default config;
