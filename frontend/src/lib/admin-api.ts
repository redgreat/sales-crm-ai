/**
 * AI 配置后台 API 客户端。
 *
 * 同源直连 /api/v1（开发环境由 vite 代理到 AI 服务，生产由 FastAPI 自身提供）。
 * 后台接口只认管理端会话 Token，不再走联调代理注入签名——这样后台能随镜像部署到生产。
 * 管理端会话用 Bearer Token，保存在 sessionStorage——关闭标签页即失效，
 * 不落 localStorage 是为了避免长期驻留一枚高权限凭据。
 */

import { base } from "$app/paths";

const BASE = "/api";
const TOKEN_KEY = "sai-admin-token";

export class ApiError extends Error {
	constructor(
		public code: string,
		message: string,
		public status: number
	) {
		super(message);
	}
}

export function getToken(): string | null {
	if (typeof sessionStorage === "undefined") return null;
	return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
	if (typeof sessionStorage === "undefined") return;
	if (token) sessionStorage.setItem(TOKEN_KEY, token);
	else sessionStorage.removeItem(TOKEN_KEY);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
	const headers: Record<string, string> = {};
	if (body !== undefined) headers["Content-Type"] = "application/json";
	const token = getToken();
	if (token) headers["Authorization"] = `Bearer ${token}`;

	let response: Response;
	try {
		response = await fetch(`${BASE}${path}`, {
			method,
			headers,
			body: body !== undefined ? JSON.stringify(body) : undefined
		});
	} catch {
		throw new ApiError("NETWORK", "无法连接 AI 服务，请检查服务是否已启动。", 0);
	}

	const raw = await response.text();
	let data: unknown = null;
	try {
		data = raw ? JSON.parse(raw) : null;
	} catch {
		data = { raw };
	}
	if (!response.ok) {
		const err = (data as { error?: { code?: string; message?: string } })?.error;
		const status = response.status;
		if (status === 401 && typeof window !== "undefined" && !path.startsWith("/v1/auth")) {
			setToken(null);
			window.location.href = `${base}/login`;
		}
		throw new ApiError(err?.code ?? `HTTP_${status}`, err?.message ?? raw, status);
	}
	return data as T;
}

/* ---------- 认证与用户 ---------- */

export type Role = "admin" | "operator" | "viewer";

export interface SessionUser {
	id: string;
	username: string;
	display_name: string;
	role: Role;
	permissions: string[];
	ver: number;
	expires_at: number;
}

export interface AdminUser {
	id: string;
	username: string;
	display_name: string;
	role: Role;
	disabled: boolean;
	created_at: string;
	updated_at: string;
	last_login_at: string | null;
	permissions: string[];
}

/* ---------- 配置快照 ---------- */

export interface SettingsValues {
	model: {
		provider: "stub" | "openai_compatible";
		base_url: string;
		name: string;
		temperature: number;
		timeout_seconds: number;
		max_retries: number;
	};
	crm: { base_url: string; timeout_seconds: number };
	asr: {
		enabled: boolean;
		workspace_id: string;
		region: string;
		model: string;
		language_hints: string[];
		diarization_enabled: boolean;
		timeout_seconds: number;
		poll_interval_seconds: number;
		poll_timeout_seconds: number;
	};
	ocr: {
		enabled: boolean;
		endpoint: string;
		type: string;
		output_coordinate: string;
		timeout_seconds: number;
	};
	oss: { enabled: boolean; endpoint: string; bucket: string; signed_url_ttl_seconds: number };
	research: Record<
		"bocha" | "qichacha",
		{
			enabled: boolean;
			url: string;
			tool_name: string;
			query_argument: string;
			timeout_seconds: number;
		}
	>;
}

export interface CredentialSpec {
	path: string;
	group: string;
	label: string;
	hint: string;
	configured: boolean;
}

export interface ManagedTarget {
	kind: string;
	target: string;
	id: string;
	name: string;
}

export interface SettingsSnapshot {
	revision: string;
	environment: "dev" | "test" | "prod";
	values: SettingsValues;
	credentials: Record<string, boolean>;
	credential_specs: CredentialSpec[];
	managed: Record<string, ManagedTarget | null>;
	restart_required: boolean;
}

/* ---------- 连接列表 ---------- */

export type ConnectionKind = "model" | "external" | "mcp";

export interface Connection {
	id: string;
	name: string;
	target: string;
	enabled: boolean;
	/** 每个凭据字段是否已配置（明文永不回显） */
	secrets_configured: Record<string, boolean>;
	created_at: string;
	updated_at: string;
	provider?: string;
	base_url?: string;
	model?: string;
	url?: string;
	tool_name?: string;
	query_argument?: string;
	key_id?: string;
	endpoint?: string;
	type?: string;
	output_coordinate?: string;
	language_hints?: string[];
	diarization_enabled?: boolean;
	bucket?: string;
	signed_url_ttl_seconds?: number;
	temperature?: number;
	timeout_seconds?: number;
	max_retries?: number;
}

export interface ConnectionInput {
	name?: string;
	target?: string;
	provider?: string;
	base_url?: string;
	model?: string;
	url?: string;
	tool_name?: string;
	query_argument?: string;
	key_id?: string;
	endpoint?: string;
	type?: string;
	output_coordinate?: string;
	language_hints?: string[];
	diarization_enabled?: boolean;
	bucket?: string;
	signed_url_ttl_seconds?: number;
	temperature?: number;
	timeout_seconds?: number;
	max_retries?: number;
	/** 凭据：只写不回显，留空的键表示不修改 */
	secrets?: Record<string, string>;
	enabled?: boolean;
}

export interface ConnectionKindSummary {
	kind: ConnectionKind;
	targets: string[];
	items: Connection[];
}

export const adminApi = {
	/* 认证 */
	async bootstrap(): Promise<{ enabled: boolean; bootstrapped: boolean }> {
		return request("GET", "/v1/auth/bootstrap");
	},

	async login(username: string, password: string): Promise<{ token: string; expires_in: number; user: SessionUser }> {
		const result = await request<{ token: string; expires_in: number; user: SessionUser }>(
			"POST",
			"/v1/auth/login",
			{ username, password }
		);
		setToken(result.token);
		return result;
	},

	async logout(): Promise<void> {
		try {
			await request("POST", "/v1/auth/logout", {});
		} finally {
			setToken(null);
		}
	},

	async me(): Promise<SessionUser> {
		return request("GET", "/v1/auth/me");
	},

	listUsers(): Promise<AdminUser[]> {
		return request("GET", "/v1/auth/users");
	},

	createUser(input: {
		username: string;
		password: string;
		role: Role;
		display_name?: string;
	}): Promise<AdminUser> {
		return request("POST", "/v1/auth/users", input);
	},

	updateUser(
		userId: string,
		input: { display_name?: string; role?: Role; disabled?: boolean }
	): Promise<AdminUser> {
		return request("PUT", `/v1/auth/users/${encodeURIComponent(userId)}`, input);
	},

	resetPassword(userId: string, password: string): Promise<{ ok: boolean }> {
		return request("POST", `/v1/auth/users/${encodeURIComponent(userId)}/password`, { password });
	},

	deleteUser(userId: string): Promise<{ deleted: string }> {
		return request("DELETE", `/v1/auth/users/${encodeURIComponent(userId)}`);
	},

	changePassword(oldPassword: string, newPassword: string): Promise<{ ok: boolean }> {
		return request("POST", "/v1/auth/me/password", {
			old_password: oldPassword,
			new_password: newPassword
		});
	},

	/* 配置 */
	getSettings(): Promise<SettingsSnapshot> {
		return request("GET", "/v1/settings");
	},

	/* 连接 */
	listAllConnections(): Promise<{ kinds: ConnectionKindSummary[] }> {
		return request("GET", "/v1/settings/connections");
	},

	listConnections(kind: string): Promise<Connection[]> {
		return request("GET", `/v1/settings/connections/${encodeURIComponent(kind)}`);
	},

	createConnection(kind: string, input: ConnectionInput): Promise<Connection> {
		return request("POST", `/v1/settings/connections/${encodeURIComponent(kind)}`, input);
	},

	getConnection(kind: string, id: string): Promise<Connection> {
		return request("GET", `/v1/settings/connections/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`);
	},

	updateConnection(kind: string, id: string, input: ConnectionInput): Promise<Connection> {
		return request("PUT", `/v1/settings/connections/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, input);
	},

	deleteConnection(kind: string, id: string): Promise<{ deleted: string }> {
		return request("DELETE", `/v1/settings/connections/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`);
	},

	async health(): Promise<boolean> {
		try {
			// 健康检查不带 /api 前缀
			return (await fetch("/health")).ok;
		} catch {
			return false;
		}
	}
};

export const FIELD_LABELS: Record<string, string> = {
	customer_name: "客户名称",
	due_date: "截止日期",
	assignee_name: "负责人"
};
