<script lang="ts">
	import { onMount } from 'svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import PageHeader from '$lib/components/admin/page-header.svelte';
	import ConfirmDialog from '$lib/components/admin/confirm-dialog.svelte';
	import EmptyState from '$lib/components/admin/empty-state.svelte';
	import Modal from '$lib/components/admin/modal.svelte';
	import TableSkeleton from '$lib/components/admin/table-skeleton.svelte';
	import { ApiError, adminApi, type AdminUser, type Role, type SessionUser } from '$lib/admin-api';

	const ROLES: { value: Role; label: string; description: string }[] = [
		{ value: 'admin', label: '管理员', description: '全部权限，可管理用户' },
		{ value: 'operator', label: '运维', description: '配置读写、密钥与连接管理' },
		{ value: 'viewer', label: '只读', description: '仅可查看配置与连接' }
	];

	let users = $state<AdminUser[]>([]);
	let me = $state<SessionUser | null>(null);
	let loading = $state(true);
	let error = $state('');
	let notice = $state('');

	// 新增表单
	let creating = $state(false);
	let draftUsername = $state('');
	let draftDisplayName = $state('');
	let draftRole = $state<Role>('viewer');
	let draftPassword = $state('');
	let saving = $state(false);
	let formError = $state('');

	// 操作目标
	let pendingReset = $state<AdminUser | null>(null);
	let resetPasswordValue = $state('');
	let resetting = $state(false);
	let pendingDelete = $state<AdminUser | null>(null);
	let deleting = $state(false);

	// 修改自己口令
	let changing = $state(false);
	let oldPassword = $state('');
	let newPassword = $state('');
	let changeError = $state('');

	onMount(() => void load());

	async function load() {
		loading = true;
		error = '';
		try {
			const [list, session] = await Promise.all([adminApi.listUsers(), adminApi.me()]);
			users = list;
			me = session;
		} catch (err) {
			error = err instanceof ApiError ? err.message : '用户列表读取失败。';
			users = [];
		} finally {
			loading = false;
		}
	}

	function resetCreateForm() {
		draftUsername = '';
		draftDisplayName = '';
		draftRole = 'viewer';
		draftPassword = '';
		formError = '';
	}

	async function submitCreate(event: SubmitEvent) {
		event.preventDefault();
		if (saving) return;
		saving = true;
		formError = '';
		try {
			await adminApi.createUser({
				username: draftUsername.trim(),
				password: draftPassword,
				role: draftRole,
				display_name: draftDisplayName.trim() || undefined
			});
			creating = false;
			resetCreateForm();
			await load();
			notice = '用户已创建。';
		} catch (err) {
			formError = err instanceof ApiError ? err.message : '创建失败，请稍后重试。';
		} finally {
			saving = false;
		}
	}

	async function changeRole(user: AdminUser, role: Role) {
		try {
			await adminApi.updateUser(user.id, { role });
			await load();
			notice = `「${user.username}」角色已更新。`;
		} catch (err) {
			error = err instanceof ApiError ? err.message : '角色更新失败。';
		}
	}

	async function toggleDisabled(user: AdminUser, disabled: boolean) {
		try {
			await adminApi.updateUser(user.id, { disabled });
			await load();
			notice = `「${user.username}」已${disabled ? '停用' : '启用'}。`;
		} catch (err) {
			error = err instanceof ApiError ? err.message : '状态更新失败。';
		}
	}

	async function confirmReset() {
		if (!pendingReset) return;
		resetting = true;
		try {
			await adminApi.resetPassword(pendingReset.id, resetPasswordValue);
			notice = `「${pendingReset.username}」口令已重置，其已登录会话立即失效。`;
			pendingReset = null;
			resetPasswordValue = '';
		} catch (err) {
			error = err instanceof ApiError ? err.message : '口令重置失败。';
			pendingReset = null;
		} finally {
			resetting = false;
		}
	}

	async function confirmDelete() {
		if (!pendingDelete) return;
		deleting = true;
		try {
			await adminApi.deleteUser(pendingDelete.id);
			notice = `「${pendingDelete.username}」已删除。`;
			pendingDelete = null;
			await load();
		} catch (err) {
			error = err instanceof ApiError ? err.message : '删除失败。';
			pendingDelete = null;
		} finally {
			deleting = false;
		}
	}

	async function submitChangePassword(event: SubmitEvent) {
		event.preventDefault();
		changeError = '';
		try {
			await adminApi.changePassword(oldPassword, newPassword);
			changing = false;
			oldPassword = '';
			newPassword = '';
			notice = '口令已修改，其它会话已失效。';
		} catch (err) {
			changeError = err instanceof ApiError ? err.message : '修改失败。';
		}
	}

	const inputClass =
		'h-8 w-full min-w-0 border border-input bg-background px-2.5 text-xs outline-none focus-visible:border-ring';
</script>

<PageHeader title="用户与权限" breadcrumb="配置中心">
	{#snippet actions()}
		<Button size="sm" variant="outline" onclick={() => (changing = true)}>修改我的口令</Button>
		<Button size="sm" variant="outline" onclick={() => void load()} disabled={loading}>刷新</Button>
		<Button size="sm" onclick={() => (creating = true)}>新增用户</Button>
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
		open={changing}
		title="修改我的口令"
		onclose={() => (changing = false)}
	>
		<form id="change-password-form" onsubmit={submitChangePassword} class="space-y-4">
			<div class="space-y-1.5">
				<Label for="old-password">当前口令</Label>
				<Input id="old-password" type="password" bind:value={oldPassword} autocomplete="current-password" />
			</div>
			<div class="space-y-1.5">
				<Label for="new-password">新口令（至少 8 位）</Label>
				<Input id="new-password" type="password" bind:value={newPassword} autocomplete="new-password" />
			</div>
			{#if changeError}
				<p role="alert" class="border-l-2 border-destructive bg-destructive/10 px-3 py-2 text-xs text-destructive">
					{changeError}
				</p>
			{/if}
		</form>
		{#snippet footer()}
			<Button type="button" size="sm" variant="outline" onclick={() => (changing = false)}>取消</Button>
			<Button
				type="submit"
				size="sm"
				form="change-password-form"
				disabled={!oldPassword || newPassword.length < 8}
			>
				确认修改
			</Button>
		{/snippet}
	</Modal>

	<Modal
		open={creating}
		title="新增用户"
		busy={saving}
		onclose={() => (creating = false)}
	>
		<form id="create-user-form" onsubmit={submitCreate} class="space-y-4">
			<div class="grid gap-4 sm:grid-cols-2">
				<div class="space-y-1.5">
					<Label for="new-username">用户名</Label>
					<Input id="new-username" bind:value={draftUsername} required />
				</div>
				<div class="space-y-1.5">
					<Label for="new-display">姓名</Label>
					<Input id="new-display" bind:value={draftDisplayName} placeholder="可留空" />
				</div>
				<div class="space-y-1.5">
					<Label for="new-role">角色</Label>
					<select id="new-role" class={inputClass} bind:value={draftRole}>
						{#each ROLES as role (role.value)}
							<option value={role.value}>{role.label}</option>
						{/each}
					</select>
				</div>
				<div class="space-y-1.5">
					<Label for="new-password-field">初始口令（至少 8 位）</Label>
					<Input id="new-password-field" type="password" bind:value={draftPassword} required />
				</div>
			</div>
			{#if formError}
				<p role="alert" class="border-l-2 border-destructive bg-destructive/10 px-3 py-2 text-xs text-destructive">
					{formError}
				</p>
			{/if}
		</form>
		{#snippet footer()}
			<Button type="button" size="sm" variant="outline" onclick={() => (creating = false)} disabled={saving}>
				取消
			</Button>
			<Button
				type="submit"
				size="sm"
				form="create-user-form"
				disabled={saving || draftPassword.length < 8 || !draftUsername}
			>
				{saving ? '创建中…' : '创建'}
			</Button>
		{/snippet}
	</Modal>

	{#if loading}
		<TableSkeleton rows={4} />
	{:else if users.length === 0}
		<EmptyState title="没有用户" />
	{:else}
		<div class="overflow-x-auto border bg-card">
			<table class="w-full min-w-[720px] text-xs">
				<thead class="border-b bg-muted/40 text-left text-muted-foreground">
					<tr>
						<th class="px-3 py-2 font-medium">用户</th>
						<th class="px-3 py-2 font-medium">角色</th>
						<th class="px-3 py-2 font-medium">状态</th>
						<th class="px-3 py-2 font-medium">最近登录</th>
						<th class="px-3 py-2 text-right font-medium">操作</th>
					</tr>
				</thead>
				<tbody>
					{#each users as user (user.id)}
						{@const isSelf = me !== null && user.id === me.id}
						<tr class="border-b last:border-b-0 hover:bg-muted/30">
							<td class="px-3 py-2">
								<div class="font-medium">
									{user.display_name || user.username}
									{#if isSelf}<span class="ml-1 text-[10px] text-muted-foreground">（我）</span>{/if}
								</div>
							</td>
							<td class="px-3 py-2">
								<select
									class={inputClass}
									value={user.role}
									disabled={isSelf}
									onchange={(event) =>
										void changeRole(user, (event.currentTarget as HTMLSelectElement).value as Role)}
								>
									{#each ROLES as role (role.value)}
										<option value={role.value}>{role.label}</option>
									{/each}
								</select>
							</td>
							<td class="px-3 py-2">
								{#if user.disabled}
									<Badge variant="destructive">已停用</Badge>
								{:else}
									<Badge variant="secondary">正常</Badge>
								{/if}
							</td>
							<td class="px-3 py-2 text-muted-foreground">
								{user.last_login_at ? new Date(user.last_login_at).toLocaleString('zh-CN') : '从未登录'}
							</td>
							<td class="px-3 py-2">
								<div class="flex justify-end gap-1">
									<Button
										size="sm"
										variant="ghost"
										disabled={isSelf}
										onclick={() => void toggleDisabled(user, !user.disabled)}
									>
										{user.disabled ? '启用' : '停用'}
									</Button>
									<Button size="sm" variant="ghost" onclick={() => (pendingReset = user)}>重置口令</Button>
									<Button size="sm" variant="ghost" disabled={isSelf} onclick={() => (pendingDelete = user)}>
										删除
									</Button>
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
	title="删除用户"
	message={pendingDelete ? `确认删除「${pendingDelete.username}」？该操作不可撤销。` : ''}
	busy={deleting}
	oncancel={() => (pendingDelete = null)}
	onconfirm={() => void confirmDelete()}
/>

<Modal
	open={pendingReset !== null}
	title={pendingReset ? `重置「${pendingReset.username}」的口令` : '重置口令'}
	busy={resetting}
	onclose={() => (pendingReset = null)}
>
	<form
		id="reset-password-form"
		onsubmit={(event) => {
			event.preventDefault();
			void confirmReset();
		}}
	>
		<div class="space-y-1.5">
			<Label for="reset-password">新口令（至少 8 位）</Label>
			<Input
				id="reset-password"
				type="password"
				bind:value={resetPasswordValue}
				autocomplete="new-password"
			/>
		</div>
	</form>
	{#snippet footer()}
		<Button type="button" size="sm" variant="outline" onclick={() => (pendingReset = null)} disabled={resetting}>
			取消
		</Button>
		<Button
			type="submit"
			size="sm"
			form="reset-password-form"
			disabled={resetting || resetPasswordValue.length < 8}
		>
			{resetting ? '重置中…' : '确认重置'}
		</Button>
	{/snippet}
</Modal>
