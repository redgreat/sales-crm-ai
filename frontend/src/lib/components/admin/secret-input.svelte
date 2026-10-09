<script lang="ts">
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';

	interface Props {
		id: string;
		label: string;
		hint?: string;
		configured?: boolean;
		disabled?: boolean;
		value?: string;
	}

	let {
		id,
		label,
		hint,
		configured = false,
		disabled = false,
		// 不能用带默认值的 $bindable：调用方一旦绑定 undefined，Svelte 会直接抛
		// props_invalid_value，整个弹窗渲染不出来
		value = $bindable()
	}: Props = $props();

	let revealed = $state(false);
</script>

<div class="space-y-1.5">
	<div class="flex items-center justify-between gap-2">
		<Label for={id}>{label}</Label>
		{#if configured}
			<span class="text-[11px] text-emerald-600 dark:text-emerald-400">已配置</span>
		{/if}
	</div>
	<div class="flex gap-2">
		<Input
			{id}
			{disabled}
			class="flex-1"
			type={revealed ? 'text' : 'password'}
			autocomplete="off"
			placeholder={configured ? '留空表示不修改' : ''}
			bind:value
		/>
		<Button type="button" size="sm" variant="outline" onclick={() => (revealed = !revealed)}>
			{revealed ? '隐藏' : '显示'}
		</Button>
	</div>
	{#if hint}
		<p class="text-[11px] text-muted-foreground">{hint}</p>
	{/if}
</div>
