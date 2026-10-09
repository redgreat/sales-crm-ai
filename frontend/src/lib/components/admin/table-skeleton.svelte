<script lang="ts">
	interface Props {
		rows?: number;
	}

	let { rows = 4 }: Props = $props();
</script>

<div class="space-y-2" aria-busy="true" aria-live="polite">
	{#each Array.from({ length: rows }) as _, index (index)}
		<div class="h-9 animate-pulse bg-muted"></div>
	{/each}
</div>
