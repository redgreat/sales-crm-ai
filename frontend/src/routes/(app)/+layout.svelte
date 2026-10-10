<script lang="ts">
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import type { Snippet } from 'svelte';
	import { adminApi, getToken, setToken, type SessionUser } from '$lib/admin-api';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { base, resolve } from '$app/paths';
	import { onMount } from 'svelte';

	let { children }: { children: Snippet } = $props();

	let me = $state<SessionUser | null>(null);
	let bootstrapping = $state(true);
	let serviceOk = $state<boolean | null>(null);
	let menuOpen = $state(false);

	const NAV = [
		{ href: '/connections/model', label: '模型服务', icon: '◆' },
		{ href: '/connections/external', label: '外部接口', icon: '⇄' },
		{ href: '/connections/mcp', label: 'MCP 服务', icon: '◇' },
		{ href: '/users', label: '用户与权限', icon: '☖' }
	];

	function can(permission: string): boolean {
		return me?.permissions.includes(permission) ?? false;
	}

	function visible(item: { href: string }) {
		if (item.href === '/users') return can('users:manage');
		return true;
	}

	onMount(() => {
		if (!getToken()) {
			void goto(resolve('/login'), { replaceState: true });
			return;
		}
		void load();
		const timer = setInterval(() => void adminApi.health().then((ok) => (serviceOk = ok)), 15_000);
		return () => clearInterval(timer);
	});

	async function load() {
		try {
			me = await adminApi.me();
			serviceOk = await adminApi.health();
		} catch {
			setToken(null);
			await goto(resolve('/login'), { replaceState: true });
		} finally {
			bootstrapping = false;
		}
	}

	async function logout() {
		await adminApi.logout();
		await goto(resolve('/login'), { replaceState: true });
	}

	const ROLE_LABEL: Record<string, string> = { admin: '管理员', operator: '运维', viewer: '只读' };
	// 挂载在 /admin 下：URL 带 base，导航项是 base 相对的路径，比较前先剥掉 base
	function toRoutePath(pathname: string): string {
		if (pathname === base) return '/';
		return pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : pathname;
	}
	let current = $derived(toRoutePath($page.url.pathname));
	let title = $derived(NAV.find((item) => current.startsWith(item.href))?.label ?? '概览');
</script>

<div class="flex min-h-screen bg-background">
	<!-- 墨蓝侧栏 -->
	<aside
		class={`fixed inset-y-0 left-0 z-40 flex w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar transition-transform lg:static lg:translate-x-0 ${
			menuOpen ? 'translate-x-0' : '-translate-x-full'
		}`}
	>
		<div class="flex h-14 items-center gap-2 border-b border-sidebar-border px-4">
			<span class="flex h-7 w-7 items-center justify-center border border-sidebar-primary text-[11px] font-semibold text-sidebar-primary">
				瑞赢
			</span>
			<div class="min-w-0 leading-tight">
				<div class="truncate text-xs font-semibold text-sidebar-foreground">AI 配置后台</div>
				<div class="truncate text-[10px] text-sidebar-foreground/50">sales-crm-ai</div>
			</div>
		</div>

		<nav class="flex-1 space-y-0.5 overflow-y-auto p-2">
			{#each NAV.filter(visible) as item (item.href)}
				{@const active = current === item.href || current.startsWith(item.href + '/')}
				<a
					href={resolve(item.href)}
					onclick={() => (menuOpen = false)}
					class={`flex items-center gap-2 border-l-2 px-3 py-2 text-xs transition-colors ${
						active
							? 'border-sidebar-primary bg-sidebar-accent font-medium text-sidebar-primary'
							: 'border-transparent text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground'
					}`}
				>
					<span class="w-3 text-center text-[11px] opacity-70">{item.icon}</span>
					<span>{item.label}</span>
				</a>
			{/each}
		</nav>

		<div class="border-t border-sidebar-border p-3 text-[10px] text-sidebar-foreground/40">
			权限由角色决定，写接口后端二次校验
		</div>
	</aside>

	{#if menuOpen}
		<div
			class="fixed inset-0 z-30 bg-black/50 lg:hidden"
			role="presentation"
			onclick={() => (menuOpen = false)}
		></div>
	{/if}

	<div class="flex min-w-0 flex-1 flex-col">
		<header class="sticky top-0 z-20 flex h-14 items-center gap-3 border-b bg-card/95 px-4 backdrop-blur">
			<Button
				variant="ghost"
				size="sm"
				class="lg:hidden"
				onclick={() => (menuOpen = !menuOpen)}
				aria-label="切换菜单"
			>
				☰
			</Button>
			<div class="min-w-0 flex-1">
				<div class="text-[11px] text-muted-foreground">配置中心 / {title}</div>
				<div class="truncate text-xs font-medium">{title}</div>
			</div>
			<Badge variant={serviceOk === null ? 'outline' : serviceOk ? 'secondary' : 'destructive'}>
				{serviceOk === null ? '检查中' : serviceOk ? '服务在线' : '服务离线'}
			</Badge>
			{#if me}
				<div class="flex items-center gap-2 border-l pl-3">
					<div class="hidden text-right leading-tight sm:block">
						<div class="text-xs">{me.display_name || me.username}</div>
						<div class="text-[10px] text-muted-foreground">{ROLE_LABEL[me.role] ?? me.role}</div>
					</div>
					<Button variant="outline" size="sm" onclick={logout}>退出</Button>
				</div>
			{/if}
		</header>

		<main class="min-w-0 flex-1 px-4 py-5 md:px-6">
			{#if bootstrapping}
				<div class="py-16 text-center text-sm text-muted-foreground">加载中…</div>
			{:else}
				{@render children()}
			{/if}
		</main>
	</div>
</div>
