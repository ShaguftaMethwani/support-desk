import type {
  CanonicalExplainer,
  ChatRequest,
  ChatResponse,
  Draft,
  LoginRequest,
  PendingAction,
  QueueItemDetail,
  QueueListItem,
  Review,
  Source,
  User,
  VoiceResponse,
  WeeklyRun,
  WeeklyRunSummary,
} from "./types";

const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") || "/api";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// Fired on any 401, so the app can send the user back to the login screen.
export const UNAUTHORIZED_EVENT = "desk:unauthorized";

function detail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((e) => (e as { msg?: string }).msg ?? String(e)).join("; ");
  }
  return fallback;
}

async function request<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, headers, ...rest } = init;
  const response = await fetch(`${BASE}${path}`, {
    ...rest,
    credentials: "include",
    headers: {
      // The API refuses state-changing cookie requests without this header (CSRF guard).
      "X-Requested-With": "desk",
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/auth/")) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    throw new ApiError(response.status, detail(body, `${response.status} ${response.statusText}`));
  }
  return body as T;
}

const post = <T>(path: string, json?: unknown) =>
  request<T>(path, { method: "POST", json: json ?? undefined });

export const api = {
  // auth
  me: () => request<User>("/auth/me"),
  login: (body: LoginRequest) => post<User>("/auth/login", body),
  logout: () => post<{ status: string }>("/auth/logout"),

  // chat
  sendMessage: (body: ChatRequest) => post<ChatResponse>("/chat/message", body),
  transcribe: (audio: Blob) =>
    request<VoiceResponse>("/chat/voice", {
      method: "POST",
      body: audio,
      headers: { "Content-Type": audio.type.split(";")[0] || "audio/webm" },
    }),

  // queue
  queue: (limit = 200) => request<{ items: QueueListItem[] }>(`/queue?limit=${limit}`),
  queueItem: (id: string) => request<QueueItemDetail>(`/queue/${id}`),
  refine: (id: string, feedback: string) =>
    post<{ round: number; feedback_type: string; agents_fired: string[]; draft: Draft }>(
      `/queue/${id}/refine`,
      { feedback },
    ),
  editDraft: (id: string, reply: string) =>
    request<{ draft: Draft }>(`/queue/${id}/draft`, { method: "PUT", json: { reply } }),
  approveItem: (id: string) =>
    post<{ status: string; pending_action_ids: string[] }>(`/queue/${id}/approve`),
  rejectItem: (id: string) => post<{ status: string }>(`/queue/${id}/reject`),
  importReviews: (reviewIds: string[]) =>
    post<{ items: { id: string; status: string; intent: string | null }[] }>(
      "/queue/import-csv",
      { review_ids: reviewIds },
    ),
  combinedReply: (messageId: string) =>
    post<PendingAction>(`/messages/${messageId}/combined-reply`),

  // approval gate
  pendingActions: () => request<{ actions: PendingAction[] }>("/actions/pending"),
  approveAction: (id: string, payloadHash: string) =>
    post<PendingAction>(`/actions/${id}/approve`, { payload_hash: payloadHash }),
  rejectAction: (id: string) => post<PendingAction>(`/actions/${id}/reject`),
  retryAction: (id: string) => post<PendingAction>(`/actions/${id}/retry`),

  // weekly loop
  weeklyRuns: () => request<{ runs: WeeklyRunSummary[] }>("/weekly/runs?limit=20"),
  weeklyRun: (id: string) => request<WeeklyRun>(`/weekly/${id}`),
  triggerWeekly: () => post<{ run_id: string; status: string }>("/weekly/run", {}),
  reviews: (ids: string[]) =>
    request<{ reviews: Review[]; missing: string[] }>(
      `/reviews?${ids.map((id) => `ids=${encodeURIComponent(id)}`).join("&")}`,
    ),

  // sources and explainers
  sources: () => request<{ sources: Source[] }>("/sources"),
  canonicalExplainers: (includeArchived: boolean) =>
    request<CanonicalExplainer[]>(`/explainers/canonical?include_archived=${includeArchived}`),
};
