<script lang="ts" module>
	// 纯客户端渲染 + 单个 index.html 回落：产物是静态 SPA，随镜像由 FastAPI 提供
	export const prerender = false;
	export const ssr = false;
</script>

<script lang="ts">
	import './layout.css';
	import favicon from '$lib/assets/favicon.svg';
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();

	// 管理端固定暗色：瑞赢主题的墨蓝侧栏在暗色下才是主视觉，不给切换入口
	$effect(() => {
		document.documentElement.classList.add('dark');
	});
</script>

<svelte:head><link rel="icon" href={favicon} /><title>瑞赢 AI 配置后台</title></svelte:head>

{@render children()}
