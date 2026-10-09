<script lang="ts">
	import { Button } from '$lib/components/ui/button';
	import type { Snippet } from 'svelte';

	interface Props {
		open: boolean;
		title: string;
		message: string;
		confirmText?: string;
		busy?: boolean;
		extra?: Snippet;
		oncancel: () => void;
		onconfirm: () => void;
	}

	let {
		open,
		title,
		message,
		confirmText = '确认删除',
		busy = false,
		extra,
		oncancel,
		onconfirm
	}: Props = $props();
</script>

{#if open}
	<div class="fixed inset-0 z-50 flex items-center justify-center p-4">
		<div
			class="absolute inset-0 bg-black/50"
			role="presentation"
			onclick={() => !busy && oncancel()}
		></div>
		<div
			class="relative w-full max-w-md border bg-card p-5 shadow-xl"
			role="dialog"
			aria-modal="true"
			aria-label={title}
		>
			<h2 class="text-sm font-semibold">{title}</h2>
			<p class="mt-2 text-xs leading-relaxed text-muted-foreground">{message}</p>
			{#if extra}
				<div class="mt-3">{@render extra()}</div>
			{/if}
			<div class="mt-5 flex justify-end gap-2">
				<Button size="sm" variant="outline" disabled={busy} onclick={oncancel}>取消</Button>
				<Button size="sm" variant="destructive" disabled={busy} onclick={onconfirm}>
					{busy ? '处理中…' : confirmText}
				</Button>
			</div>
		</div>
	</div>
{/if}
