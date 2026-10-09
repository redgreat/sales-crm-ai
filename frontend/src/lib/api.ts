/**
 * AI 联调前端 API 客户端。
 * 全部经 /playground/api 同源代理访问（开发代理在服务端签名注入联调身份，
 * 浏览器不持任何密钥）；生产环境由 CRM 同源集成接口替代。
 */

const BASE = "/playground/api";

export type RunStatus =
	| "queued"
	| "running"
	| "waiting_input"
	| "succeeded"
	| "failed"
	| "cancelled";

export class ApiError extends Error {
	constructor(
		public code: string,
		message: string,
		public status: number
	) {
		super(message);
	}
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
	const response = await fetch(`${BASE}${path}`, {
		method,
		headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
		body: body !== undefined ? JSON.stringify(body) : undefined
	});
	const raw = await response.text();
	let data: unknown = null;
	try {
		data = raw ? JSON.parse(raw) : null;
	} catch {
		data = { raw };
	}
	if (!response.ok) {
		const err = (data as { error?: { code?: string; message?: string } })?.error;
		throw new ApiError(err?.code ?? `HTTP_${response.status}`, err?.message ?? raw, response.status);
	}
	return data as T;
}

export interface Run {
	run_id: string;
	capability: string;
	status: RunStatus;
	idempotent_replay?: boolean;
	attempt_count: number;
	max_attempts: number;
	result?: {
		candidates?: {
			activities: { subject: string; occurred_at?: string | null }[];
			tasks: {
				title: string;
				due_date?: string;
				assignee_name?: string;
				customer_name?: string;
			}[];
		};
		references?: Record<string, unknown>;
		notes?: string;
	};
	error?: { code: string; message: string } | null;
	status_history: { status: string; at: string; [k: string]: unknown }[];
	created_at?: string;
	finished_at?: string | null;
}

export interface PendingInfo {
	waiting: boolean;
	run_id?: string;
	questions?: { task_title: string; field: string }[];
	state_version?: number;
}

export interface Message {
	seq: number;
	role: "user" | "assistant" | "system";
	content: string;
	run_id?: string | null;
	meta?: { candidates?: Run["result"] };
	created_at?: string;
}

export interface Conversation {
	conversation_id: string;
	thread_id: string;
	status: string;
}

export interface ServiceCheck {
	status: string;
	checks?: Record<string, { ok: boolean; [k: string]: unknown }>;
}

export interface SettingsValues {
	model: { provider: "stub" | "openai_compatible"; base_url: string; name: string; temperature: number; timeout_seconds: number; max_retries: number };
	crm: { base_url: string; timeout_seconds: number };
	asr: { enabled: boolean; workspace_id: string; region: string; model: string; language_hints: string[]; diarization_enabled: boolean; timeout_seconds: number; poll_interval_seconds: number; poll_timeout_seconds: number };
	ocr: { enabled: boolean; endpoint: string; type: string; output_coordinate: string; timeout_seconds: number };
	oss: { enabled: boolean; endpoint: string; bucket: string; signed_url_ttl_seconds: number };
	research: Record<"bocha" | "qichacha", { enabled: boolean; url: string; tool_name: string; query_argument: string; timeout_seconds: number }>;
}

export interface SettingsSnapshot {
	revision: string;
	environment: "dev" | "test" | "prod";
	values: SettingsValues;
	credentials: Record<string, boolean>;
	restart_required: boolean;
}

function newIdempotencyKey(prefix: string): string {
	return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export const api = {
	getSettings(): Promise<SettingsSnapshot> {
		return request("GET", "/v1/settings");
	},

	saveSettings(revision: string, values: SettingsValues): Promise<SettingsSnapshot> {
		return request("PUT", "/v1/settings", { revision, values });
	},
	async health(): Promise<boolean> {
		try {
			return (await fetch("/playground/health")).ok;
		} catch {
			return false;
		}
	},

	async ready(): Promise<ServiceCheck | null> {
		try {
			const response = await fetch("/playground/ready");
			return (await response.json()) as ServiceCheck;
		} catch {
			return null;
		}
	},

	createRun(text: string): Promise<Run> {
		return request<Run>("POST", "/v1/runs", {
			capability: "communication.extract",
			input: { text },
			idempotency_key: newIdempotencyKey("pg")
		});
	},

	getRun(runId: string): Promise<Run> {
		return request<Run>("GET", `/v1/runs/${encodeURIComponent(runId)}`);
	},

	getRunPending(runId: string): Promise<PendingInfo> {
		return request<PendingInfo>("GET", `/v1/runs/${encodeURIComponent(runId)}/pending`);
	},

	cancelRun(runId: string): Promise<{ run_id: string; status: string }> {
		return request("POST", `/v1/runs/${encodeURIComponent(runId)}/cancel`, {});
	},

	resumeRun(
		runId: string,
		values: Record<string, unknown>,
		stateVersion: number
	): Promise<Run> {
		return request("POST", `/v1/runs/${encodeURIComponent(runId)}/resume`, {
			values,
			state_version: stateVersion
		});
	},

	createConversation(): Promise<Conversation> {
		return request("POST", "/v1/conversations", {});
	},

	listMessages(conversationId: string): Promise<{ messages: Message[] }> {
		return request("GET", `/v1/conversations/${encodeURIComponent(conversationId)}/messages`);
	},

	async postMessage(
		conversationId: string,
		text: string
	): Promise<{ mode: "resume" | "new_run"; run_id: string; status: string }> {
		const body = { text, idempotency_key: newIdempotencyKey("conv") };
		return request("POST", `/v1/conversations/${encodeURIComponent(conversationId)}/messages`, body);
	},

	getConversationPending(conversationId: string): Promise<PendingInfo> {
		return request("GET", `/v1/conversations/${encodeURIComponent(conversationId)}/pending`);
	},

	closeConversation(conversationId: string): Promise<{ conversation_id: string; status: string }> {
		return request("POST", `/v1/conversations/${encodeURIComponent(conversationId)}/close`, {});
	}
};

/** 轮询直到 Run 到达终态或等待态；返回最后一次快照。 */
export async function pollRun(
	runId: string,
	onTick: (run: Run) => void,
	timeoutMs = 60_000
): Promise<Run> {
	const deadline = Date.now() + timeoutMs;
	let last!: Run;
	while (Date.now() < deadline) {
		last = await api.getRun(runId);
		onTick(last);
		if (["succeeded", "failed", "cancelled"].includes(last.status)) return last;
		if (last.status === "waiting_input") return last;
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new ApiError("POLL_TIMEOUT", "等待超时，请稍后手动刷新", 0);
}

export const FIELD_LABELS: Record<string, string> = {
	customer_name: "客户名称",
	due_date: "截止日期",
	assignee_name: "负责人"
};
