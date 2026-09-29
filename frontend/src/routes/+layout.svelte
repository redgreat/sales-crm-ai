<script lang="ts">
	import './layout.css';
	import favicon from '$lib/assets/favicon.svg';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { api } from '$lib/api';

	let { children } = $props();

	let dark = $state(true);
	let serviceOk = $state<boolean | null>(null);

	$effect(() => {
		const saved = typeof localStorage !== 'undefined' ? localStorage.getItem('theme') : null;
		setDark(saved !== 'light');
		const refresh = async () => {
			serviceOk = await api.health();
		};
		refresh();
		const timer = setInterval(refresh, 10_000);
		return () => clearInterval(timer);
	});

	function setDark(value: boolean) {
		dark = value;
		document.documentElement.classList.toggle('dark', value);
		localStorage.setItem('theme', value ? 'dark' : 'light');
	}
</script>

<svelte:head><link rel="icon" href={favicon} /><title>sales-crm-ai 联调台</title></svelte:head>

<div class="flex min-h-screen flex-col">
	<header class="flex h-12 items-center justify-between border-b px-4">
		<div class="flex items-center gap-3">
			<span class="text-sm font-semibold tracking-wide">sales-crm-ai</span>
			<Badge variant="secondary">开发联调</Badge>
			<Badge variant={serviceOk === null ? 'outline' : serviceOk ? 'secondary' : 'destructive'}>
				{serviceOk === null ? '检查中' : serviceOk ? '服务在线' : '服务离线'}
			</Badge>
		</div>
		<Button variant="ghost" size="sm" onclick={() => setDark(!dark)}>
			{dark ? '切换亮色' : '切换暗色'}
		</Button>
	</header>
	<main class="mx-auto w-full max-w-4xl flex-1 px-4 py-6">
		{@render children()}
	</main>
	<footer class="border-t px-4 py-2 text-xs text-muted-foreground">
		浏览器不经手任何服务密钥；请求经本机开发代理签名转发。生产环境由 CRM 同源集成接口提供。
	</footer>
</div>
