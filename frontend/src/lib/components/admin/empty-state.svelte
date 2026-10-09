<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		description?: string;
		action?: Snippet;
	}

	let { title, description, action }: Props = $props();
</script>

<div class="flex flex-col items-center justify-center gap-3 border border-dashed px-6 py-14 text-center">
	<p class="text-sm font-medium">{title}</p>
	{#if description}
		<p class="max-w-sm text-xs text-muted-foreground">{description}</p>
	{/if}
	{#if action}
		<div class="mt-1">{@render action()}</div>
	{/if}
</div>
