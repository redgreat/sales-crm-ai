<script lang="ts">
	import * as Card from '$lib/components/ui/card';
	import { Badge } from '$lib/components/ui/badge';
	import { Separator } from '$lib/components/ui/separator';
	import { FIELD_LABELS, type Run } from '$lib/api';

	let { run }: { run: Run } = $props();

	const tasks = $derived(run.result?.candidates?.tasks ?? []);
	const activities = $derived(run.result?.candidates?.activities ?? []);
</script>

<Card.Root>
	<Card.Header>
		<Card.Title class="text-sm">候选建议（未写入正式业务，需本人确认）</Card.Title>
	</Card.Header>
	<Card.Content class="space-y-4">
		{#if activities.length > 0}
			<div>
				<p class="mb-1 text-xs font-medium text-muted-foreground">活动建议</p>
				<ul class="space-y-1 text-sm">
					{#each activities as activity}
						<li class="flex items-center gap-2">
							<span>{activity.subject}</span>
							{#if activity.occurred_at}
								<Badge variant="outline">{activity.occurred_at}</Badge>
							{/if}
						</li>
					{/each}
				</ul>
			</div>
		{/if}
		{#if tasks.length > 0}
			<div>
				<p class="mb-1 text-xs font-medium text-muted-foreground">任务建议</p>
				<ul class="space-y-2 text-sm">
					{#each tasks as task}
						<li class="border px-3 py-2">
							<p class="font-medium">{task.title}</p>
							<p class="mt-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
								{#if task.customer_name}<span>客户：{task.customer_name}</span>{/if}
								{#if task.due_date}<span>截止：{task.due_date}</span>{/if}
								{#if task.assignee_name}<span>负责人：{task.assignee_name}</span>{/if}
							</p>
						</li>
					{/each}
				</ul>
			</div>
		{/if}
		{#if tasks.length === 0 && activities.length === 0}
			<p class="text-sm text-muted-foreground">无候选内容</p>
		{/if}
		<Separator />
		<div class="flex flex-wrap gap-2 text-xs text-muted-foreground">
			{#each Object.entries(run.result?.references?.customers ?? []) as [index, name]}
				<Badge variant="outline">引用：{name}</Badge>
			{/each}
		</div>
		{#if run.result?.notes}
			<p class="text-xs text-muted-foreground">{run.result.notes}</p>
		{/if}
	</Card.Content>
</Card.Root>

<p class="sr-only">{FIELD_LABELS.customer_name}</p>
