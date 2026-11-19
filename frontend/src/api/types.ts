// Response shapes. The backend returns plain dicts for most endpoints, so FastAPI's OpenAPI
// schema has no response models to generate from: these mirror the view functions in
// backend/app/api/*.py. Request bodies come from the generated schema (schema.d.ts).
import type { components } from "./schema";

export type Schemas = components["schemas"];
export type ChatRequest = Schemas["ChatRequest"];
export type LoginRequest = Schemas["LoginRequest"];

export interface User {
  id: string;
  username: string;
  role: string;
}

export interface Citation {
  url: string;
  title: string | null;
  page: number | null;
  chunk_id?: string;
  source_key?: string;
  last_checked: string | null;
}

export interface Routing {
  team: string;
  action: string;
  reason: string;
  priority: string;
  disagreement: string | null;
}

export type SourceType =
  | "corpus"
  | "canonical_explainer"
  | "refusal"
  | "not_covered"
  | "clarification";

export interface Draft {
  id: string;
  version: number;
  reply: string;
  spoken_text: string;
  citation: Citation | null;
  last_checked: string | null;
  source_type: SourceType;
  canonical_explainer_id: string | null;
  routing: Routing | null;
  author: "system" | "human";
  model_tag: string | null;
  review_status: "passed" | "blocked" | "rework";
  approvable: boolean;
}

export type ItemStatus =
  | "new"
  | "drafting"
  | "awaiting_human"
  | "blocked"
  | "approved"
  | "rejected"
  | "unavailable";

export interface QueueItem {
  id: string;
  message_id: string;
  part_index: number;
  question: string;
  channel: "text" | "voice" | "csv" | null;
  intent: string | null;
  intent_confidence: number | null;
  slots: Record<string, unknown>;
  status: ItemStatus;
  created_at: string | null;
  draft: Draft | null;
}

export interface ComplianceFlag {
  type: string;
  detail: string;
  source: "guard" | "rule" | "model";
}

export interface ComplianceOutput {
  incoming_flags: ComplianceFlag[];
  risk_level: "low" | "medium" | "high";
  notes: string;
  model_used: boolean;
}

export interface QueueListItem extends QueueItem {
  compliance: ComplianceOutput | null;
  pattern: { label: "recurring" | "rising" | "one_off"; theme: string | null } | null;
}

export interface RuleResult {
  rule: string;
  passed: boolean;
  detail: string | null;
}

export interface AgentRun {
  agent: string;
  status: "ok" | "error" | "cached";
  started_at: string;
  finished_at: string | null;
  latency_ms: number | null;
  draft_version_id: string | null;
  output: Record<string, unknown> | null;
}

export interface QueueItemDetail extends QueueItem {
  versions: (Draft & { review: RuleResult[] | null })[];
  agent_runs: AgentRun[];
  refinements: { round: number; feedback_type: string; agents_fired: string[] }[];
}

export interface ChatResponse {
  session_id: string;
  message_id: string;
  split_method: string;
  answers: QueueItem[];
  dropped: { message: string; questions: string[] } | null;
}

export interface SchemeSuggestion {
  heard: string;
  start: number; // character offsets in the transcript
  end: number;
  suggestion: string;
  scheme: string;
  scheme_name: string;
  score: number;
}

export interface VoiceResponse {
  transcript: string; // already redacted
  pii_flags: string[];
  suggestions: SchemeSuggestion[];
  duration_seconds: number | null;
  channel: "voice";
}

export interface PendingAction {
  id: string;
  kind: "doc_append" | "email_draft" | "promote_canonical" | string;
  origin: string;
  origin_id: string;
  target: string;
  payload: Record<string, unknown>;
  payload_hash: string;
  status: "pending" | "approved" | "executed" | "failed" | "rejected" | string;
  result_id: string | null;
  error: string | null;
  created_at: string | null;
  executed_at: string | null;
  approval: { approver_id: string; approved_at: string | null } | null;
}

export interface Window {
  start: string;
  end: string;
}

export interface WeeklyRunSummary {
  run_id: string;
  status: "pending" | "in_progress" | "awaiting_approval" | "failed" | string;
  error: string | null;
  triggered_by: string;
  window: Window;
  started_at: string | null;
  finished_at: string | null;
}

export interface Theme {
  id: string;
  rank: number | null;
  label: string;
  description: string | null;
  count: number;
  review_ids: string[];
  question_ids: string[];
  from_support_queue: boolean;
  window: Window;
}

export interface Quote {
  review_id: string;
  theme_id: string;
  theme: string;
  text: string;
}

export interface FeeCount {
  label: string;
  fee_type: string;
  reviews: number;
  review_ids: string[];
  mutual_fund: boolean;
}

export interface WeeklyRun extends WeeklyRunSummary {
  stages: Record<string, string>;
  stage_log: Record<string, unknown>;
  themes: Theme[];
  quotes: Quote[];
  fees: { label?: string | null; counts?: FeeCount[] } & Record<string, unknown>;
  pulse: { text: string; word_count: number } | null;
  explainer: {
    fee_type: string;
    bullets: string[];
    source_urls: string[];
    last_checked: string;
    evidence_review_ids: string[];
  } | null;
  actions: { id: string; kind: string; status: string; target: string; payload_hash: string }[];
}

export interface Review {
  review_id: string;
  source: string;
  date: string;
  rating: number | null;
  text: string;
}

export interface Source {
  id: string;
  key: string;
  url: string;
  publisher: string;
  doc_type: string;
  title: string | null;
  schemes: string[];
  last_checked: string | null;
  chunks: number;
  citing_count: number;
  citing: {
    draft_id: string;
    item_id: string;
    version: number;
    question: string;
    item_status: ItemStatus;
    source_type: SourceType;
    review_status: string;
    page: number | null;
    created_at: string | null;
  }[];
}

export interface CanonicalExplainer {
  id: string;
  fee_type: string;
  label: string;
  active: boolean;
  bullets: string[];
  source_urls: string[];
  last_checked: string;
  explainer_id: string;
  weekly_run_id: string;
  approved_at: string;
  approved_by: string | null;
  archived_at: string | null;
  reuse_count: number;
}
