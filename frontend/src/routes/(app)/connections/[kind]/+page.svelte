<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import PageHeader from '$lib/components/admin/page-header.svelte';
	import ConfirmDialog from '$lib/components/admin/confirm-dialog.svelte';
	import EmptyState from '$lib/components/admin/empty-state.svelte';
	import Modal from '$lib/components/admin/modal.svelte';
	import SecretInput from '$lib/components/admin/secret-input.svelte';
	import TableSkeleton from '$lib/components/admin/table-skeleton.svelte';
	import { ApiError, adminApi, type Connection, type ConnectionInput } from '$lib/admin-api';

	interface FieldSpec {
		key: string;
		label: string;
		type?: 'text' | 'url' | 'number' | 'select' | 'checkbox' | 'tags';
		options?: string[];
		optionLabels?: Record<string, string>;
		placeholder?: string;
		step?: string;
		span?: boolean;
	}

	interface SecretSpec {
		key: string;
		label: string;
	}

	interface TargetSpec {
		value: string;
		label: string;
		fields: FieldSpec[];
		secrets: SecretSpec[];
		summary: (item: Connection) => string;
	}

	const url = (item: Connection): string => item.url ?? '-';
	const noSecret = (label: string): SecretSpec[] => [{ key: 'secret', label }];

	const TARGETS: Record<string, TargetSpec> = {
		model: {
			value: 'model',
			label: '默认模型',
			summary: (item) => `${item.base_url ?? '-'} · ${item.model ?? '-'}`,
			fields: [
				{ key: 'provider', label: 'Provider', type: 'select', options: ['stub', 'openai_compatible'] },
				{ key: 'model', label: '模型名称', placeholder: 'qwen-plus' },
				{ key: 'base_url', label: 'API 地址', type: 'url', placeholder: 'https://api.example.com/v1', span: true },
				{ key: 'temperature', label: '温度', type: 'number', step: '0.1' },
				{ key: 'timeout_seconds', label: '请求超时（秒）', type: 'number', step: '1' },
				{ key: 'max_retries', label: '重试次数', type: 'number', step: '1' }
			],
			secrets: noSecret('API Key')
		},
		crm: {
			value: 'crm',
			label: 'CRM 接口',
			summary: (item) => item.base_url ?? '-',
			fields: [
				{ key: 'base_url', label: '服务地址', type: 'url', placeholder: 'https://salescrm-api-service.lunztech.cn/api/v1/salescrm', span: true },
				{ key: 'key_id', label: '签名 Key ID', placeholder: 'ai-crm' },
				{ key: 'timeout_seconds', label: '超时（秒）', type: 'number', step: '1' },
				{ key: 'token_endpoint', label: 'OAuth Token 地址', type: 'url', placeholder: 'https://identity-fat.lunz.cn/connect/token', span: true },
				{ key: 'client_id', label: 'OAuth Client ID', placeholder: 'salescrm-client' },
				{ key: 'scopes', label: 'OAuth Scopes', placeholder: 'salescrm uc-users-outside-api', span: true }
			],
			secrets: [
				{ key: 'secret', label: '签名密钥' },
				{ key: 'client_secret', label: 'OAuth 客户端密钥（/h5 联调页登录用）' }
			]
		},
		ocr: {
			value: 'ocr',
			label: 'OCR 文字识别',
			summary: (item) => `${item.endpoint ?? '-'} · ${item.type ?? '-'}`,
			fields: [
				{ key: 'endpoint', label: 'Endpoint', placeholder: 'ocr-api.cn-hangzhou.aliyuncs.com', span: true },
				{ key: 'type', label: '识别类型', type: 'select', options: ['Advanced', 'Basic'] },
				{ key: 'output_coordinate', label: '坐标格式', type: 'select', options: ['points', 'rectangle', 'none'] },
				{ key: 'timeout_seconds', label: '超时（秒）', type: 'number', step: '1' }
			],
			secrets: [
				{ key: 'access_key_id', label: 'AccessKey ID' },
				{ key: 'access_key_secret', label: 'AccessKey Secret' }
			]
		},
		asr: {
			value: 'asr',
			label: 'ASR 语音识别',
			summary: (item) =>
				`${item.model ?? '-'}${item.language_hints?.length ? ` · ${item.language_hints.join('/')}` : ''}`,
			fields: [
				{ key: 'model', label: '模型', placeholder: 'paraformer-v2' },
				{ key: 'language_hints', label: '语言（逗号分隔）', type: 'tags', placeholder: 'zh,en' },
				{ key: 'diarization_enabled', label: '说话人分离', type: 'checkbox' }
			],
			secrets: noSecret('API Key')
		},
		oss: {
			value: 'oss',
			label: 'OSS 对象存储',
			summary: (item) => `${item.endpoint ?? '-'} · ${item.bucket ?? '-'}`,
			fields: [
				{ key: 'endpoint', label: 'Endpoint', placeholder: 'oss-cn-qingdao.aliyuncs.com', span: true },
				{ key: 'bucket', label: 'Bucket', placeholder: 'sales-crm-test' },
				{ key: 'signed_url_ttl_seconds', label: '签名 URL 有效期（秒）', type: 'number', step: '1' }
			],
			secrets: [
				{ key: 'access_key_id', label: 'AccessKey ID' },
				{ key: 'access_key_secret', label: 'AccessKey Secret' }
			]
		},
		bocha: {
			value: 'bocha',
			label: '博查',
			summary: (item) => `${item.url ?? '-'} · ${item.tool_name ?? '-'}`,
			fields: [
				{ key: 'tool_name', label: '工具名', placeholder: 'web_search' },
				{ key: 'url', label: 'MCP 地址', type: 'url', placeholder: 'https://mcp.example.com/sse', span: true },
				{ key: 'query_argument', label: '查询参数名', placeholder: 'query' },
				{ key: 'timeout_seconds', label: '超时（秒）', type: 'number', step: '1' }
			],
			secrets: noSecret('API Key')
		},
		qichacha: {
			value: 'qichacha',
			label: '企查查',
			summary: (item) => `${item.url ?? '-'} · ${item.tool_name ?? '-'}`,
			fields: [
				{ key: 'tool_name', label: '工具名', placeholder: 'qcc_search' },
				{ key: 'url', label: 'MCP 地址', type: 'url', placeholder: 'https://mcp.example.com/sse', span: true },
				{ key: 'query_argument', label: '查询参数名', placeholder: 'query' },
				{ key: 'timeout_seconds', label: '超时（秒）', type: 'number', step: '1' }
			],
			secrets: noSecret('API Key')
		}
	};

	const KIND_META: Record<string, { title: string; targets: string[] }> = {
		model: { title: '模型服务', targets: ['model'] },
		external: {
			title: '外部接口',
			targets: ['crm', 'ocr', 'asr', 'oss']
		},
		mcp: {
			title: 'MCP 服务',
			targets: ['bocha', 'qichacha']
		}
	};

	let kind = $derived($page.params.kind ?? 'model');
	let meta = $derived(KIND_META[kind]);
	let items = $state<Connection[]>([]);
	let loading = $state(true);
	let error = $state('');
	let notice = $state('');
	let canManage = $state(false);
	let readonly = $derived(!canManage);

	// 编辑弹窗
	let editing = $state<Connection | null>(null);
	let creating = $state(false);
	let draft = $state<ConnectionInput>({});
	let draftSecrets = $state<Record<string, string>>({});
	let saving = $state(false);
	let formError = $state('');

	// 删除确认
	let pendingDelete = $state<Connection | null>(null);
	let deleting = $state(false);

	let targetOptions = $derived(
		(meta?.targets ?? []).map((value) => ({ value, label: TARGETS[value]?.label ?? value }))
	);
	let targetSpec = $derived(TARGETS[draft.target ?? meta?.targets[0] ?? ''] ?? TARGETS[meta?.targets[0] ?? '']);
	let fields = $derived(targetSpec?.fields ?? []);
	let secretSpecs = $derived(targetSpec?.secrets ?? []);

	onMount(() => {
		void loadPermissions();
	});

	$effect(() => {
		// kind 变化时重新拉取
		void kind;
		void load();
	});

	async function loadPermissions() {
		try {
			const me = await adminApi.me();
			canManage = me.permissions.includes('connections:manage');
		} catch {
			canManage = false;
		}
	}

	async function load() {
		loading = true;
		error = '';
		try {
			items = await adminApi.listConnections(kind);
		} catch (err) {
			error = err instanceof ApiError ? err.message : '连接列表读取失败。';
			items = [];
		} finally {
			loading = false;
		}
	}

	// 下拉与开关必须有初值，否则弹窗里会出现空选项 / 未定状态
	function applyFieldDefaults(target: string | undefined, draft: ConnectionInput) {
		for (const field of TARGETS[target ?? '']?.fields ?? []) {
			if (draft[field.key as keyof ConnectionInput] !== undefined) continue;
			if (field.type === 'checkbox') draft[field.key as 'diarization_enabled'] = false;
			else if (field.type === 'select')
				draft[field.key as keyof ConnectionInput] = (field.options?.[0] ?? '') as never;
		}
		return draft;
	}

	function blankDraft(): ConnectionInput {
		const base: ConnectionInput = { enabled: true };
		if (kind === 'model') base.provider = 'openai_compatible';
		base.target = meta?.targets[0];
		return applyFieldDefaults(base.target, base);
	}

	// 凭据输入框必须每个键都有初值：绑定 undefined 会让 Svelte 直接抛错（弹窗渲染不出来）
	function emptySecrets(target?: string): Record<string, string> {
		const specs = TARGETS[target ?? '']?.secrets ?? [];
		return Object.fromEntries(specs.map((spec) => [spec.key, '']));
	}

	function startCreate() {
		creating = true;
		editing = null;
		formError = '';
		const next = blankDraft();
		draft = next;
		draftSecrets = emptySecrets(next.target);
	}

	function startEdit(item: Connection) {
		editing = item;
		creating = false;
		formError = '';
		const next: ConnectionInput = { name: item.name, target: item.target, enabled: item.enabled };
		for (const field of TARGETS[item.target]?.fields ?? []) {
			const value = item[field.key as keyof Connection];
			if (value === undefined) continue;
			// tags 输入框用逗号分隔字符串，提交时再拆回数组
			const key = field.key as keyof ConnectionInput;
			next[key] = (field.type === 'tags' ? (value as string[]).join(',') : value) as never;
		}
		draft = applyFieldDefaults(item.target, next);
		draftSecrets = emptySecrets(item.target);
	}

	function changeTarget() {
		// 分类切换后业务字段整体换套，旧的清掉
		const target = draft.target;
		const next: ConnectionInput = { name: draft.name, target, enabled: draft.enabled ?? true };
		if (kind === 'model') next.provider = 'openai_compatible';
		draft = applyFieldDefaults(target, next);
		draftSecrets = emptySecrets(target);
	}

	function closeForm() {
		creating = false;
		editing = null;
		draftSecrets = {};
		formError = '';
	}

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (saving) return;
		saving = true;
		formError = '';
		try {
			const payload: ConnectionInput = { ...draft };
			// tags 字段：逗号分隔字符串 → 数组
			for (const field of fields) {
				if (field.type === 'tags' && typeof payload[field.key as 'language_hints'] === 'string') {
					const text = payload[field.key as 'language_hints'] as unknown as string;
					payload[field.key as 'language_hints'] = text
						.split(',')
						.map((part) => part.trim())
						.filter(Boolean) as never;
				}
			}
			const secrets = Object.fromEntries(
				Object.entries(draftSecrets).filter(([, value]) => value.trim())
			);
			if (Object.keys(secrets).length) payload.secrets = secrets;
			if (editing) await adminApi.updateConnection(kind, editing.id, payload);
			else await adminApi.createConnection(kind, payload);
			closeForm();
			await load();
			notice = editing ? '连接已更新。' : '连接已创建并启用。';
		} catch (err) {
			formError = err instanceof ApiError ? err.message : '保存失败，请稍后重试。';
		} finally {
			saving = false;
		}
	}

	async function toggleEnabled(item: Connection, enabled: boolean) {
		error = '';
		notice = '';
		try {
			await adminApi.updateConnection(kind, item.id, { enabled });
			await load();
			notice = enabled ? `「${item.name}」已设为默认。` : `「${item.name}」已停用。`;
		} catch (err) {
			error =
				err instanceof ApiError && err.status === 422
					? err.message
					: '切换失败，请检查该连接的凭据是否齐备。';
		}
	}

	async function confirmDelete() {
		if (!pendingDelete) return;
		deleting = true;
		try {
			await adminApi.deleteConnection(kind, pendingDelete.id);
			notice = `「${pendingDelete.name}」已删除。`;
			pendingDelete = null;
			await load();
		} catch (err) {
			error = err instanceof ApiError ? err.message : '删除失败。';
			pendingDelete = null;
		} finally {
			deleting = false;
		}
	}

	function summary(item: Connection): string {
		return TARGETS[item.target]?.summary(item) ?? '-';
	}

	const inputClass =
		'h-8 w-full min-w-0 border border-input bg-background px-2.5 text-xs outline-none focus-visible:border-ring';
</script>

<PageHeader title={meta?.title ?? '连接'} breadcrumb="配置中心">
	{#snippet actions()}
		<Button size="sm" variant="outline" onclick={() => void load()} disabled={loading}>
			{loading ? '刷新中…' : '刷新'}
		</Button>
		{#if canManage}
			<Button size="sm" onclick={startCreate}>新增连接</Button>
		{/if}
	{/snippet}
</PageHeader>

<div class="mt-5 space-y-4">
	{#if error}
		<p role="alert" class="border-l-2 border-destructive bg-destructive/10 px-3 py-2 text-xs text-destructive">
			{error}
		</p>
	{/if}
	{#if notice}
		<p role="status" class="border-l-2 border-emerald-500 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400">
			{notice}
		</p>
	{/if}

	<Modal
		open={creating || editing !== null}
		title={editing ? `编辑「${editing.name}」` : `新增${meta?.title ?? ''}连接`}
		busy={saving}
		onclose={closeForm}
	>
		<form id="connection-form" onsubmit={submit} class="space-y-4">
			<div class="grid gap-4 sm:grid-cols-2">
				<div class="space-y-1.5">
					<Label for="conn-name">{kind === 'mcp' ? '连接名称' : '名称'}</Label>
					<Input id="conn-name" bind:value={draft.name} required disabled={readonly} />
				</div>
				{#if (meta?.targets ?? []).length > 1}
					<div class="space-y-1.5">
						<Label for="conn-target">分类</Label>
						<select
							id="conn-target"
							class={inputClass}
							bind:value={draft.target}
							onchange={changeTarget}
							disabled={readonly}
						>
							{#each targetOptions as option (option.value)}
								<option value={option.value}>{option.label}</option>
							{/each}
						</select>
					</div>
				{/if}
				{#each fields as field (field.key)}
					<div class={`space-y-1.5 ${field.span ? 'sm:col-span-2' : ''}`}>
						<Label for={`conn-${field.key}`}>{field.label}</Label>
						{#if field.type === 'select'}
							<select
								id={`conn-${field.key}`}
								class={inputClass}
								bind:value={draft[field.key as keyof ConnectionInput]}
								disabled={readonly}
							>
								{#each field.options ?? [] as option (option)}
									<option value={option}>{field.optionLabels?.[option] ?? option}</option>
								{/each}
							</select>
						{:else if field.type === 'checkbox'}
							<label class="flex h-8 items-center gap-2 text-xs">
								<input type="checkbox" bind:checked={draft[field.key as 'diarization_enabled']} disabled={readonly} />
								<span class="text-muted-foreground">开启</span>
							</label>
						{:else}
							<Input
								id={`conn-${field.key}`}
								type={field.type === 'number' ? 'number' : (field.type === 'url' ? 'url' : 'text')}
								step={field.step}
								placeholder={field.placeholder}
								bind:value={draft[field.key as keyof ConnectionInput]}
								disabled={readonly}
							/>
						{/if}
					</div>
				{/each}

				{#each secretSpecs as spec (spec.key)}
					<div class={secretSpecs.length > 1 ? 'sm:col-span-2' : ''}>
						<SecretInput
							id={`conn-secret-${spec.key}`}
							label={spec.label}
							configured={editing?.secrets_configured?.[spec.key] ?? false}
							bind:value={draftSecrets[spec.key]}
							disabled={readonly}
						/>
					</div>
				{/each}
			</div>

			{#if formError}
				<p role="alert" class="border-l-2 border-destructive bg-destructive/10 px-3 py-2 text-xs text-destructive">
					{formError}
				</p>
			{/if}
		</form>

		{#snippet footer()}
			<Button type="button" size="sm" variant="outline" onclick={closeForm} disabled={saving}>取消</Button>
			{#if !readonly}
				<Button type="submit" size="sm" form="connection-form" disabled={saving || !draft.name}>
					{saving ? '保存中…' : '保存'}
				</Button>
			{/if}
		{/snippet}
	</Modal>

	{#if loading}
		<TableSkeleton rows={4} />
	{:else if items.length === 0}
		<EmptyState title={`还没有${meta?.title ?? ''}连接`}>
			{#snippet action()}
				{#if canManage}
					<Button size="sm" onclick={startCreate}>新增连接</Button>
				{/if}
			{/snippet}
		</EmptyState>
	{:else}
		<div class="overflow-x-auto border bg-card">
			<table class="w-full min-w-[640px] text-xs">
				<thead class="border-b bg-muted/40 text-left text-muted-foreground">
					<tr>
						<th class="px-3 py-2 font-medium">名称</th>
						<th class="px-3 py-2 font-medium">接入信息</th>
						<th class="px-3 py-2 font-medium">凭据</th>
						<th class="px-3 py-2 font-medium">状态</th>
						<th class="px-3 py-2 text-right font-medium">操作</th>
					</tr>
				</thead>
				<tbody>
					{#each items as item (item.id)}
						<tr class="border-b last:border-b-0 hover:bg-muted/30">
							<td class="px-3 py-2">
								<div class="font-medium">{item.name}</div>
								<div class="text-[11px] text-muted-foreground">
									分类：{TARGETS[item.target]?.label ?? item.target}
								</div>
							</td>
							<td class="max-w-[280px] truncate px-3 py-2 text-muted-foreground" title={summary(item)}>
								{summary(item)}
							</td>
							<td class="px-3 py-2">
								{#if Object.values(item.secrets_configured ?? {}).some(Boolean)}
									<Badge variant="secondary">已配置</Badge>
								{:else}
									<Badge variant="destructive">未配置</Badge>
								{/if}
							</td>
							<td class="px-3 py-2">
								{#if canManage}
									<label class="flex items-center gap-1.5">
										<input
											type="checkbox"
											checked={item.enabled}
											onchange={(event) =>
												void toggleEnabled(item, (event.currentTarget as HTMLInputElement).checked)}
										/>
										<span class={item.enabled ? 'text-emerald-500' : 'text-muted-foreground'}>
											{item.enabled ? '启用' : '停用'}
										</span>
									</label>
								{:else}
									<Badge variant={item.enabled ? 'secondary' : 'outline'}>
										{item.enabled ? '启用' : '停用'}
									</Badge>
								{/if}
							</td>
							<td class="px-3 py-2">
								<div class="flex justify-end gap-1">
									{#if canManage}
										<Button size="sm" variant="ghost" onclick={() => startEdit(item)}>编辑</Button>
										<Button size="sm" variant="ghost" onclick={() => (pendingDelete = item)}>删除</Button>
									{:else}
										<Button size="sm" variant="ghost" onclick={() => startEdit(item)}>查看</Button>
									{/if}
								</div>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>

<ConfirmDialog
	open={pendingDelete !== null}
	title="删除连接"
	message={pendingDelete
		? `确认删除「${pendingDelete.name}」？删除后该连接的配置不再生效。`
		: ''}
	busy={deleting}
	oncancel={() => (pendingDelete = null)}
	onconfirm={() => void confirmDelete()}
/>
