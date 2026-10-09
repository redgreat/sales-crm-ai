<script lang="ts">
	import type { Snippet } from 'svelte';
	import { Button } from '$lib/components/ui/button';

	interface Props {
		open: boolean;
		title: string;
		busy?: boolean;
		wide?: boolean;
		onclose: () => void;
		children: Snippet;
		footer?: Snippet;
	}

	let { open, title, busy = false, wide = false, onclose, children, footer }: Props =
		$props();

	function onkeydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && open && !busy) onclose();
	}
</script>

<svelte:window onkeydown={onkeydown} />

{#if open}
	<div
		class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:p-10"
	>
		<!-- 点击遮罩关闭 -->
		<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
		<div class="absolute inset-0" aria-hidden="true" onclick={() => !busy && onclose()}></div>

		<div
			class={`relative w-full ${wide ? 'max-w-3xl' : 'max-w-xl'} border bg-card shadow-2xl`}
			role="dialog"
			aria-modal="true"
			aria-label={title}
		>
			<div class="flex items-start justify-between gap-4 border-b px-5 py-3.5">
				<div class="min-w-0">
					<h2 class="text-sm font-semibold">{title}</h2>
				</div>
				<Button type="button" size="sm" variant="ghost" onclick={onclose} disabled={busy} aria-label="关闭">
					✕
				</Button>
			</div>

			<div class="max-h-[70vh] overflow-y-auto px-5 py-4">
				{@render children()}
			</div>

			{#if footer}
				<div class="flex justify-end gap-2 border-t px-5 py-3">
					{@render footer()}
				</div>
			{/if}
		</div>
	</div>
{/if}
