<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import PageHeader from '$lib/components/admin/page-header.svelte';
	import { ApiError, adminApi, type Connection, type SettingsSnapshot } from '$lib/admin-api';

	let snapshot = $state<SettingsSnapshot | null>(null);
	let total = $state(0);
	let enabled = $state(0);
	let loading = $state(true);
	let error = $state('');

	const TARGET_LABEL: Record<string, string> = {
		model: '模型服务',
		crm: 'CRM 接口',
		ocr: 'OCR 文字识别',
		asr: 'ASR 语音识别',
		oss: 'OSS 对象存储',
		'research.bocha': '博查 MCP',
		'research.qichacha': '企查查 MCP'
	};

	// 由后台接管的密钥 → 对应连接页；未列出的由 config.yml / 环境变量提供
	const SECRET_TARGET: Record<string, string> = {
		'model.api_key': '/connections/model',
		'crm.secret': '/connections/external',
		'ocr.access_key_id': '/connections/external',
		'ocr.access_key_secret': '/connections/external',
		'asr.api_key': '/connections/external',
		'oss.access_key_id': '/connections/external',
		'oss.access_key_secret': '/connections/external',
		'research.bocha.api_key': '/connections/mcp',
		'research.qichacha.api_key': '/connections/mcp'
	};

	onMount(() => void load());

	async function load() {
		loading = true;
		error = '';
		try {
			const [settings, summary] = await Promise.all([
				adminApi.getSettings(),
				adminApi.listAllConnections()
			]);
			snapshot = settings;
			const items: Connection[] = summary.kinds.flatMap((kind) => kind.items);
			total = items.length;
			enabled = items.filter((item) => item.enabled).length;
		} catch (err) {
			error = err instanceof ApiError ? err.message : '概览数据读取失败。';
		} finally {
			loading = false;
		}
	}

	let missing = $derived(
		(snapshot?.credential_specs ?? []).filter((spec) => !spec.configured)
	);
	let managed = $derived(Object.entries(snapshot?.managed ?? {}).filter(([, value]) => value));
</script>

<PageHeader title="概览" breadcrumb="配置中心">
	{#snippet actions()}
		<Button size="sm" variant="outline" onclick={() => void load()} disabled={loading}>
			{loading ? '刷新中…' : '刷新'}
		</Button>
	{/snippet}
</PageHeader>

<div class="mt-5 space-y-5">
	{#if error}
		<p role="alert" class="border-l-2 border-destructive bg-destructive/10 px-3 py-2 text-xs text-destructive">
			{error}
		</p>
	{/if}

	{#if loading}
		<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
			{#each Array.from({ length: 4 }) as _, index (index)}
				<div class="h-20 animate-pulse bg-muted"></div>
			{/each}
		</div>
	{:else if snapshot}
		<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
			<div class="border bg-card p-4">
				<div class="text-[11px] text-muted-foreground">运行环境</div>
				<div class="mt-1 text-lg font-semibold">{snapshot.environment}</div>
			</div>
			<div class="border bg-card p-4">
				<div class="text-[11px] text-muted-foreground">连接</div>
				<div class="mt-1 text-lg font-semibold">{enabled} / {total}</div>
			</div>
			<div class="border bg-card p-4">
				<div class="text-[11px] text-muted-foreground">密钥</div>
				<div class="mt-1 text-lg font-semibold">
					{(snapshot.credential_specs.length - missing.length)} / {snapshot.credential_specs.length}
				</div>
			</div>
			<div class="border bg-card p-4">
				<div class="text-[11px] text-muted-foreground">由连接接管</div>
				<div class="mt-1 text-lg font-semibold">{managed.length}</div>
			</div>
		</div>

		<div class="grid gap-4 lg:grid-cols-2">
			<section class="border bg-card">
				<div class="border-b px-4 py-2.5 text-xs font-semibold">生效中的连接</div>
				<div class="p-4">
					{#if managed.length === 0}
						<p class="text-xs text-muted-foreground">暂无启用的连接。</p>
					{:else}
						<ul class="space-y-2">
							{#each managed as [key, value] (key)}
								<li class="flex items-center justify-between gap-3 border-b pb-2 last:border-b-0 last:pb-0">
									<span class="text-xs text-muted-foreground">{TARGET_LABEL[key] ?? key}</span>
									<span class="flex items-center gap-2 text-xs">
										<span class="font-medium">{value?.name}</span>
										<Badge variant="secondary">启用</Badge>
									</span>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</section>

			<section class="border bg-card">
				<div class="border-b px-4 py-2.5 text-xs font-semibold">待补齐的密钥</div>
				<div class="p-4">
					{#if missing.length === 0}
						<p class="text-xs text-muted-foreground">所有支持的密钥项均已配置。</p>
					{:else}
						<ul class="space-y-2">
							{#each missing as spec (spec.path)}
								{@const href = SECRET_TARGET[spec.path] ?? '/connections/model'}
								<li class="flex items-start justify-between gap-3 border-b pb-2 last:border-b-0 last:pb-0">
									<span class="block text-xs">{spec.label}</span>
									{#if SECRET_TARGET[spec.path]}
										<a
											href={`${base}${href}`}
											class="shrink-0 text-xs text-primary underline underline-offset-2"
										>
											去连接页填写
										</a>
									{:else}
										<Badge variant="destructive">未配置</Badge>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</section>
		</div>
	{/if}
</div>
