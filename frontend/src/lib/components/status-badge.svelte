<script lang="ts">
	import { Badge } from '$lib/components/ui/badge';
	import type { RunStatus } from '$lib/api';

	let { status }: { status: RunStatus | string } = $props();

	const mapping: Record<string, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline'; pulse?: boolean }> = {
		queued: { label: '排队中', variant: 'secondary' },
		running: { label: '执行中', variant: 'secondary', pulse: true },
		waiting_input: { label: '等待补参', variant: 'outline' },
		succeeded: { label: '已完成', variant: 'default' },
		failed: { label: '失败', variant: 'destructive' },
		cancelled: { label: '已取消', variant: 'outline' }
	};

	const item = $derived(mapping[status] ?? { label: status, variant: 'outline' as const });
</script>

	<Badge variant={item.variant} class={item.pulse ? 'animate-pulse' : ''}>{item.label}</Badge>
