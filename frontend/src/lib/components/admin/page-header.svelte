<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		breadcrumb?: string;
		actions?: Snippet;
	}

	let { title, breadcrumb, actions }: Props = $props();
</script>

<div class="flex flex-wrap items-end justify-between gap-3 border-b pb-4">
	<div class="min-w-0 space-y-1">
		{#if breadcrumb}
			<div class="text-xs text-muted-foreground">{breadcrumb}</div>
		{/if}
		<h1 class="text-lg font-semibold tracking-tight">{title}</h1>
	</div>
	{#if actions}
		<div class="flex shrink-0 items-center gap-2">{@render actions()}</div>
	{/if}
</div>
