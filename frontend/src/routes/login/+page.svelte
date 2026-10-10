<script lang="ts">
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { ApiError, adminApi, getToken } from '$lib/admin-api';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { onMount } from 'svelte';

	let username = $state('');
	let password = $state('');
	let loading = $state(false);
	let error = $state('');
	let hint = $state('');

	onMount(() => {
		if (getToken()) void goto(`${base}/`, { replaceState: true });
	});

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (loading) return;
		loading = true;
		error = '';
		hint = '';
		try {
			await adminApi.login(username.trim(), password);
			await goto(`${base}/`, { replaceState: true });
		} catch (err) {
			if (err instanceof ApiError && err.status === 401) {
				error = '用户名或口令不正确。';
			} else if (err instanceof ApiError && err.status === 404) {
				// 后端 404 有两种：后台未启用 / 路由不存在（旧进程）。优先透传后端原文。
				const detail = err.message?.trim() ?? '';
				error =
					detail && detail !== 'Not Found' && !detail.startsWith('"')
						? detail
						: '登录接口不存在：后端可能是改动前的旧进程，请重启 AI 服务后再试。';
			} else if (err instanceof ApiError && err.status === 422) {
				error = err.message;
			} else if (err instanceof ApiError && err.status === 0) {
				error = err.message;
			} else {
				error = '登录失败，请稍后重试。';
			}
		} finally {
			loading = false;
		}
	}

	async function checkBootstrap() {
		try {
			const state = await adminApi.bootstrap();
			if (state.enabled && !state.bootstrapped) {
				hint = '尚未创建任何账号：已自动生成管理员账号 admin，初始口令见 AI 服务目录下的 conf/admin.bootstrap.txt';
			}
		} catch {
			// 引导探测失败不影响登录尝试
		}
	}

	onMount(checkBootstrap);
</script>

<div class="flex min-h-screen items-center justify-center bg-[#071018] px-4 py-10">
	<div class="w-full max-w-md">
		<div class="mb-8 text-center">
			<div
				class="mx-auto mb-4 flex h-14 w-14 items-center justify-center border border-[#c9a96e]/40 bg-[#0a1628] text-lg font-semibold text-[#c9a96e]"
			>
				瑞赢
			</div>
			<h1 class="text-2xl font-semibold text-[#f7f0e3]">AI 配置后台</h1>
			<p class="mt-2 text-base text-[#f7f0e3]/55">模型服务 · 外部接口 · MCP · 密钥管理</p>
		</div>

		<form
			onsubmit={submit}
			class="space-y-5 border border-[#c9a96e]/22 bg-[#102036] p-8"
		>
			<div class="space-y-2">
				<Label for="login-username" class="text-base text-[#f7f0e3]">用户名</Label>
				<Input id="login-username" bind:value={username} autocomplete="username" class="h-11 text-base" />
			</div>
			<div class="space-y-2">
				<Label for="login-password" class="text-base text-[#f7f0e3]">口令</Label>
				<Input
					id="login-password"
					type="password"
					bind:value={password}
					autocomplete="current-password"
					class="h-11 text-base"
				/>
			</div>

			{#if error}
				<p
					role="alert"
					class="border-l-2 border-destructive bg-destructive/10 px-3 py-2.5 text-sm leading-relaxed text-destructive"
				>
					{error}
				</p>
			{/if}
			{#if hint}
				<p class="border-l-2 border-[#c9a96e] bg-[#c9a96e]/10 px-3 py-2.5 text-sm leading-relaxed text-[#e0c896]">
					{hint}
				</p>
			{/if}

			<Button type="submit" class="h-11 w-full text-base" disabled={loading || !username || !password}>
				{loading ? '登录中…' : '登录'}
			</Button>
		</form>

		<p class="mt-5 text-center text-sm text-[#f7f0e3]/40">
			会话 2 小时后自动过期，关闭标签页立即失效
		</p>
	</div>
</div>
