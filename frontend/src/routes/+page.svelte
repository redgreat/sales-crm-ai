<script lang="ts">
	import * as Tabs from '$lib/components/ui/tabs';
	import * as Card from '$lib/components/ui/card';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Textarea } from '$lib/components/ui/textarea';
	import StatusBadge from '$lib/components/status-badge.svelte';
	import ResultCard from '$lib/components/result-card.svelte';
	import {
		api,
		pollRun,
		FIELD_LABELS,
		type Message,
		type PendingInfo,
		type Run
	} from '$lib/api';

	const SAMPLE_FULL = `客户：ACME公司
今日拜访采购负责人，谈到下季度合作计划。
任务：电话回访采购负责人
负责人：张三
2026-10-08`;

	const SAMPLE_MISSING = `客户：华信集团
电话沟通了续约意向。
任务：拜访客户总监
负责人：李四`;

	const PLANNED_CAPABILITIES: { name: string; desc: string }[] = [
		{ name: 'daily.draft', desc: '日报底稿' },
		{ name: 'object.summary', desc: '客户/线索/商机摘要' },
		{ name: 'object.qa', desc: '对象内追问' },
		{ name: 'business.qa', desc: '跨对象问答' },
		{ name: 'knowledge.qa', desc: '知识问答' },
		{ name: 'meeting.prepare', desc: '会前准备' },
		{ name: 'today.summary', desc: '今日任务总结' },
		{ name: 'manager.focus', desc: '主管关注' }
	];

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	// ---------- 沟通抽取 ----------
	let inputText = $state(SAMPLE_FULL);
	let run = $state<Run | null>(null);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let pending = $state<PendingInfo | null>(null);
	let answers = $state<Record<string, string>>({});

	const groupedQuestions = $derived.by(() => {
		const groups: { title: string; fields: string[] }[] = [];
		for (const q of pending?.questions ?? []) {
			let group = groups.find((x) => x.title === q.task_title);
			if (!group) {
				group = { title: q.task_title, fields: [] };
				groups.push(group);
			}
			group.fields.push(q.field);
		}
		return groups;
	});

	function buildStructuredValues(): Record<string, unknown> {
		const perTask: Record<string, Record<string, string>> = {};
		for (const [key, value] of Object.entries(answers)) {
			if (!value.trim()) continue;
			const [title, field] = key.split('|');
			(perTask[title] ??= { title })[field] = value.trim();
		}
		return { tasks: Object.values(perTask) };
	}

	async function submitExtract() {
		busy = true;
		error = null;
		pending = null;
		run = null;
		answers = {};
		try {
			let current = await api.createRun(inputText);
			run = current;
			current = await pollRun(current.run_id, (snapshot) => (run = snapshot));
			if (current.status === 'waiting_input') {
				pending = await api.getRunPending(current.run_id);
			}
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	async function submitResume() {
		if (!run || !pending?.state_version) return;
		busy = true;
		error = null;
		try {
			await api.resumeRun(run.run_id, buildStructuredValues(), pending.state_version);
			pending = null;
			answers = {};
			let current = await pollRun(run.run_id, (snapshot) => (run = snapshot));
			if (current.status === 'waiting_input') {
				pending = await api.getRunPending(current.run_id);
			}
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	async function cancelCurrent() {
		if (!run) return;
		try {
			await api.cancelRun(run.run_id);
			run = await api.getRun(run.run_id);
			pending = null;
		} catch (err) {
			error = message(err);
		}
	}

	// ---------- 会话多轮 ----------
	let conversationId = $state<string | null>(null);
	let conversationClosed = $state(false);
	let messages = $state<Message[]>([]);
	let chatInput = $state('');
	let chatBusy = $state(false);
	let chatError = $state<string | null>(null);
	let convRun = $state<Run | null>(null);
	let convPending = $state<PendingInfo | null>(null);
	let convAnswers = $state<Record<string, string>>({});

	async function newConversation() {
		chatError = null;
		convPending = null;
		convRun = null;
		conversationClosed = false;
		try {
			const created = await api.createConversation();
			conversationId = created.conversation_id;
			messages = [];
		} catch (err) {
			chatError = message(err);
		}
	}

	async function refreshMessages() {
		if (!conversationId) return;
		const data = await api.listMessages(conversationId);
		messages = data.messages;
	}

	async function sendChat() {
		if (!conversationId || !chatInput.trim() || conversationClosed) return;
		chatBusy = true;
		chatError = null;
		try {
			const text = chatInput.trim();
			chatInput = '';
			const res = await api.postMessage(conversationId, text);
			await refreshMessages();
			let current = await pollRun(res.run_id, (snapshot) => (convRun = snapshot));
			if (current.status === 'waiting_input') {
				convPending = await api.getConversationPending(conversationId);
				convAnswers = {};
			} else {
				convPending = null;
			}
			await refreshMessages();
		} catch (err) {
			chatError = message(err);
		} finally {
			chatBusy = false;
		}
	}

	async function submitConvAnswers() {
		if (!conversationId || !convPending?.state_version) return;
		chatBusy = true;
		chatError = null;
		try {
			const lines = Object.entries(convAnswers)
				.filter(([, value]) => value.trim())
				.map(([key, value]) => `${FIELD_LABELS[key.split('|')[1]] ?? key}：${value.trim()}`)
				.join('\n');
			convPending = null;
			convAnswers = {};
			const res = await api.postMessage(conversationId, lines);
			let current = await pollRun(res.run_id, (snapshot) => (convRun = snapshot));
			if (current.status === 'waiting_input') {
				convPending = await api.getConversationPending(conversationId);
			}
			await refreshMessages();
		} catch (err) {
			chatError = message(err);
		} finally {
			chatBusy = false;
		}
	}

	async function closeConversation() {
		if (!conversationId) return;
		try {
			await api.closeConversation(conversationId);
			conversationClosed = true;
		} catch (err) {
			chatError = message(err);
		}
	}

	// ---------- Run 查询 ----------
	let queryRunId = $state('');
	let queriedRun = $state<Run | null>(null);
	let queryError = $state<string | null>(null);
	let queryBusy = $state(false);

	async function queryRun() {
		if (!queryRunId.trim()) return;
		queryBusy = true;
		queryError = null;
		queriedRun = null;
		try {
			queriedRun = await api.getRun(queryRunId.trim());
		} catch (err) {
			queryError = message(err);
		} finally {
			queryBusy = false;
		}
	}
</script>

<Tabs.Root value="extract">
	<Tabs.List>
		<Tabs.Trigger value="extract">沟通抽取</Tabs.Trigger>
		<Tabs.Trigger value="conversation">会话多轮</Tabs.Trigger>
		<Tabs.Trigger value="query">Run 查询</Tabs.Trigger>
	</Tabs.List>

	<!-- 沟通抽取 -->
	<Tabs.Content value="extract" class="mt-4 space-y-4">
		<Card.Root>
			<Card.Header>
				<Card.Title class="text-sm">输入沟通记录（communication.extract）</Card.Title>
				<Card.Description class="text-xs">
					文字/粘贴来源由 CRM 装配事实后进入抽取；此处为联调直填。
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-3">
				<Textarea bind:value={inputText} rows={6} class="font-mono text-xs" />
				<div class="flex flex-wrap items-center gap-2">
					<Button size="sm" onclick={submitExtract} disabled={busy || !inputText.trim()}>
						{busy ? '提交中…' : '提交抽取'}
					</Button>
					<Button size="sm" variant="outline" onclick={() => (inputText = SAMPLE_FULL)} disabled={busy}>
						样例：信息完整
					</Button>
					<Button size="sm" variant="outline" onclick={() => (inputText = SAMPLE_MISSING)} disabled={busy}>
						样例：缺日期（触发追问）
					</Button>
				</div>
			</Card.Content>
		</Card.Root>

		{#if error}
			<p class="border border-destructive px-3 py-2 text-sm text-destructive">{error}</p>
		{/if}

		{#if run}
			<Card.Root>
				<Card.Header>
					<div class="flex items-center justify-between">
						<Card.Title class="text-sm">Run {run.run_id.slice(0, 8)}</Card.Title>
						<div class="flex items-center gap-2">
							<StatusBadge status={run.status} />
							{#if ['queued', 'running', 'waiting_input'].includes(run.status)}
								<Button size="sm" variant="ghost" onclick={cancelCurrent}>取消</Button>
							{/if}
						</div>
					</div>
				</Card.Header>
				<Card.Content class="space-y-3">
					<p class="text-xs text-muted-foreground">
						尝试 {run.attempt_count}/{run.max_attempts}
						{#if run.error}<span class="text-destructive">
							· 错误 {run.error.code}: {run.error.message}
						</span>{/if}
					</p>

					{#if run.status === 'succeeded' && run.result}
						<ResultCard {run} />
					{/if}

					{#if pending?.waiting && groupedQuestions.length > 0}
						<div class="space-y-3 border p-3">
							<p class="text-sm font-medium">请补充以下信息</p>
							{#each groupedQuestions as group (group.title)}
								<div>
									<p class="mb-1 text-xs text-muted-foreground">任务「{group.title}」</p>
									<div class="grid gap-2 sm:grid-cols-3">
										{#each group.fields as field (field)}
											<div class="space-y-1">
												<Label for={`${group.title}|${field}`} class="text-xs">
													{FIELD_LABELS[field] ?? field}
												</Label>
												<Input
													id={`${group.title}|${field}`}
													type={field === 'due_date' ? 'date' : 'text'}
													bind:value={answers[`${group.title}|${field}`]}
												/>
											</div>
										{/each}
									</div>
								</div>
							{/each}
							<Button size="sm" onclick={submitResume} disabled={busy}>提交应答</Button>
						</div>
					{/if}
				</Card.Content>
			</Card.Root>
		{/if}
	</Tabs.Content>

	<!-- 会话多轮 -->
	<Tabs.Content value="conversation" class="mt-4 space-y-4">
		<Card.Root>
			<Card.Header>
				<div class="flex items-center justify-between">
					<Card.Title class="text-sm">对象绑定会话（多轮补参）</Card.Title>
					<div class="flex gap-2">
						<Button size="sm" variant="outline" onclick={newConversation} disabled={chatBusy}>
							新建会话
						</Button>
						{#if conversationId && !conversationClosed}
							<Button size="sm" variant="ghost" onclick={closeConversation} disabled={chatBusy}>
								结束会话
							</Button>
						{/if}
					</div>
				</div>
				<Card.Description class="text-xs">
					{#if conversationId}
						会话 {conversationId.slice(0, 8)}{conversationClosed ? '（已结束）' : ''}
					{:else}
						每条消息对应独立 Run；会话等待补参时，下一条消息自动转为恢复请求。
					{/if}
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-3">
				{#if conversationId}
					<div class="max-h-72 space-y-2 overflow-y-auto border p-3">
						{#each messages as msg (msg.seq)}
							<div class="flex {msg.role === 'user' ? 'justify-end' : 'justify-start'}">
								<div
									class="max-w-[80%] px-3 py-1.5 text-sm {msg.role === 'user'
										? 'bg-primary text-primary-foreground'
										: 'bg-muted'}"
								>
									<p class="whitespace-pre-wrap">{msg.content}</p>
								</div>
							</div>
						{:else}
							<p class="text-xs text-muted-foreground">还没有消息，发送一条沟通记录开始。</p>
						{/each}
					</div>

					{#if convPending?.waiting}
						<div class="space-y-3 border p-3">
							<p class="text-sm font-medium">会话等待补参（Run {convPending.run_id?.slice(0, 8)}）</p>
							{#each convPending.questions ?? [] as q, index (index)}
								<div class="grid gap-2 sm:grid-cols-3">
									<div class="space-y-1">
										<Label for="conv-{index}" class="text-xs">
											{q.task_title} · {FIELD_LABELS[q.field] ?? q.field}
										</Label>
										<Input
											id="conv-{index}"
											type={q.field === 'due_date' ? 'date' : 'text'}
											bind:value={convAnswers[`${q.task_title}|${q.field}`]}
										/>
									</div>
								</div>
							{/each}
							<Button size="sm" onclick={submitConvAnswers} disabled={chatBusy}>提交应答</Button>
						</div>
					{:else if !conversationClosed}
						<div class="flex gap-2">
							<Textarea
								bind:value={chatInput}
								rows={2}
								placeholder="输入沟通记录（可多行），Enter 发送，Shift+Enter 换行"
								onkeydown={(event) => {
									if (event.key === 'Enter' && !event.shiftKey && !chatBusy) {
										event.preventDefault();
										sendChat();
									}
								}}
							/>
							<Button size="sm" onclick={sendChat} disabled={chatBusy || !chatInput.trim()}>
								发送
							</Button>
						</div>
					{/if}

					{#if convRun}
						<div class="flex items-center gap-2 text-xs text-muted-foreground">
							最近 Run {convRun.run_id.slice(0, 8)} <StatusBadge status={convRun.status} />
						</div>
						{#if convRun.status === 'succeeded' && convRun.result}
							<ResultCard run={convRun} />
						{/if}
					{/if}
				{:else}
					<p class="text-sm text-muted-foreground">点击「新建会话」开始。</p>
				{/if}

				{#if chatError}
					<p class="border border-destructive px-3 py-2 text-sm text-destructive">{chatError}</p>
				{/if}
			</Card.Content>
		</Card.Root>
	</Tabs.Content>

	<!-- Run 查询 -->
	<Tabs.Content value="query" class="mt-4 space-y-4">
		<Card.Root>
			<Card.Header>
				<Card.Title class="text-sm">按 Run ID 查询</Card.Title>
			</Card.Header>
			<Card.Content class="space-y-3">
				<div class="flex gap-2">
					<Input bind:value={queryRunId} placeholder="run_id (UUID)" />
					<Button size="sm" onclick={queryRun} disabled={queryBusy || !queryRunId.trim()}>
						查询
					</Button>
				</div>
				{#if queryError}
					<p class="border border-destructive px-3 py-2 text-sm text-destructive">{queryError}</p>
				{/if}
				{#if queriedRun}
					<div class="space-y-2 text-sm">
						<div class="flex items-center gap-2">
							<StatusBadge status={queriedRun.status} />
							<Badge variant="outline">{queriedRun.capability}</Badge>
							<span class="text-xs text-muted-foreground">
								尝试 {queriedRun.attempt_count}/{queriedRun.max_attempts}
							</span>
						</div>
						{#if queriedRun.error}
							<p class="text-xs text-destructive">
								{queriedRun.error.code}: {queriedRun.error.message}
							</p>
						{/if}
						<div>
							<p class="mb-1 text-xs font-medium text-muted-foreground">状态历史</p>
							<ol class="space-y-1 text-xs text-muted-foreground">
								{#each queriedRun.status_history as entry, index (index)}
									<li>
										{entry.at} · {entry.status}{entry.reason ? `（${entry.reason}）` : ''}
									</li>
								{/each}
							</ol>
						</div>
						{#if queriedRun.result}
							<ResultCard run={queriedRun} />
						{/if}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
	</Tabs.Content>
</Tabs.Root>

<Card.Root class="mt-6">
	<Card.Header>
		<Card.Title class="text-sm">规划中的能力（未实现，不提供演示）</Card.Title>
		<Card.Description class="text-xs">
			以下能力按实施计划（P4/P5）逐步交付，当前未实现，不提供任何模拟结果。
		</Card.Description>
	</Card.Header>
	<Card.Content class="flex flex-wrap gap-2">
		{#each PLANNED_CAPABILITIES as capability (capability.name)}
			<Badge variant="outline" class="opacity-60">
				{capability.name} · {capability.desc}（未实现）
			</Badge>
		{/each}
	</Card.Content>
</Card.Root>
