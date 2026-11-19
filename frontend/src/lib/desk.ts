// Pure helpers shared by the views. Covered by desk.test.ts.
import type { AgentRun, QueueItem, QueueListItem, SchemeSuggestion } from "../api/types";

export type AnswerStatus = "answered" | "refused" | "not_covered" | "blocked" | "unavailable" | "pending";

/** The status shown on an answer block: answered, refused, not covered or blocked. */
export function answerStatus(item: Pick<QueueItem, "status" | "draft">): AnswerStatus {
  if (item.status === "unavailable") return "unavailable";
  const d = item.draft;
  if (!d) return "pending";
  if (item.status === "blocked" || d.review_status === "blocked") return "blocked";
  if (d.source_type === "refusal") return "refused";
  if (d.source_type === "not_covered") return "not_covered";
  return "answered";
}

export const ANSWER_LABEL: Record<AnswerStatus, string> = {
  answered: "Answered",
  refused: "Refused",
  not_covered: "Not covered",
  blocked: "Blocked",
  unavailable: "Temporarily unavailable",
  pending: "Drafting",
};

/** The reply without its trailing Source and Last checked lines, which are shown as a chip. */
export function replyBody(reply: string): string {
  return reply
    .split("\n")
    .filter((line) => !line.startsWith("Source:") && !line.startsWith("Last checked:"))
    .join("\n")
    .trim();
}

export interface MessageGroup {
  messageId: string;
  channel: QueueListItem["channel"];
  createdAt: string | null;
  items: QueueListItem[];
}

/** Queue items grouped under their parent message, newest message first, parts in order. */
export function groupByMessage(items: QueueListItem[]): MessageGroup[] {
  const groups = new Map<string, MessageGroup>();
  for (const item of items) {
    let g = groups.get(item.message_id);
    if (!g) {
      g = { messageId: item.message_id, channel: item.channel, createdAt: item.created_at, items: [] };
      groups.set(item.message_id, g);
    }
    g.items.push(item);
    if (item.created_at && (!g.createdAt || item.created_at < g.createdAt)) g.createdAt = item.created_at;
  }
  const out = [...groups.values()];
  for (const g of out) g.items.sort((a, b) => a.part_index - b.part_index);
  out.sort((a, b) => (b.createdAt ?? "").localeCompare(a.createdAt ?? ""));
  return out;
}

export interface TimingBar {
  agent: string;
  status: string;
  startMs: number; // from the earliest start in the round
  durationMs: number;
}

export interface TimingRound {
  draftVersionId: string | null;
  totalMs: number;
  bars: TimingBar[];
}

/** Agent runs grouped by the draft they fed, each as offsets from that round's first start. */
export function timingRounds(runs: AgentRun[]): TimingRound[] {
  const byDraft = new Map<string, AgentRun[]>();
  for (const r of runs) {
    const key = r.draft_version_id ?? "none";
    byDraft.set(key, [...(byDraft.get(key) ?? []), r]);
  }
  const rounds: TimingRound[] = [];
  for (const [key, group] of byDraft) {
    const t0 = Math.min(...group.map((r) => Date.parse(r.started_at)));
    const bars = group.map((r) => {
      const start = Date.parse(r.started_at);
      const end = r.finished_at ? Date.parse(r.finished_at) : start + (r.latency_ms ?? 0);
      return { agent: r.agent, status: r.status, startMs: start - t0, durationMs: Math.max(0, end - start) };
    });
    const totalMs = Math.max(1, ...bars.map((b) => b.startMs + b.durationMs));
    rounds.push({ draftVersionId: key === "none" ? null : key, totalMs, bars });
  }
  return rounds;
}

/** Replace the misheard words of one suggestion, shifting the offsets of the ones after it. */
export function applySuggestion(
  text: string,
  suggestions: SchemeSuggestion[],
  index: number,
): { text: string; suggestions: SchemeSuggestion[] } {
  const s = suggestions[index];
  if (!s || text.slice(s.start, s.end) !== s.heard) {
    // The user edited the transcript since: drop the suggestion rather than corrupt the text.
    return { text, suggestions: suggestions.filter((_, i) => i !== index) };
  }
  const shift = s.suggestion.length - (s.end - s.start);
  const next = text.slice(0, s.start) + s.suggestion + text.slice(s.end);
  const rest = suggestions
    .filter((_, i) => i !== index)
    .map((o) => (o.start >= s.end ? { ...o, start: o.start + shift, end: o.end + shift } : o));
  return { text: next, suggestions: rest };
}

/** "Q: <question>. A: <answer>" for each answer, read aloud by speechSynthesis. */
export function spokenScript(answers: { question: string; spoken: string }[]): string[] {
  return answers.map((a, i) => {
    const prefix = answers.length > 1 ? `Question ${i + 1}: ` : "You asked: ";
    const q = a.question.trim().replace(/[?.!]*$/, "");
    return `${prefix}${q}. ${a.spoken}`;
  });
}

const DATE = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric" });
const DATETIME = new Intl.DateTimeFormat("en-IN", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return Number.isNaN(d.getTime()) ? iso : DATE.format(d);
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : DATETIME.format(d);
}

export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  const s = value.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
