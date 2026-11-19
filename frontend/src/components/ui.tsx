import { useEffect, useRef, type ReactNode } from "react";
import type { Citation } from "../api/types";
import { ANSWER_LABEL, fmtDate, hostname, type AnswerStatus } from "../lib/desk";

type Tone = "slate" | "green" | "amber" | "red" | "blue" | "violet" | "accent";

const TONES: Record<Tone, string> = {
  slate: "bg-slate-100 text-slate-700 ring-slate-200",
  green: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  amber: "bg-amber-50 text-amber-900 ring-amber-200",
  red: "bg-red-50 text-red-800 ring-red-200",
  blue: "bg-sky-50 text-sky-800 ring-sky-200",
  violet: "bg-violet-50 text-violet-800 ring-violet-200",
  accent: "bg-accent-50 text-accent-800 ring-accent-100",
};

export function Badge({ tone = "slate", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

const ANSWER_TONE: Record<AnswerStatus, Tone> = {
  answered: "green",
  refused: "amber",
  not_covered: "blue",
  blocked: "red",
  unavailable: "slate",
  pending: "slate",
};

export function AnswerBadge({ status }: { status: AnswerStatus }) {
  return <Badge tone={ANSWER_TONE[status]}>{ANSWER_LABEL[status]}</Badge>;
}

const ITEM_TONE: Record<string, Tone> = {
  awaiting_human: "accent",
  blocked: "red",
  approved: "green",
  rejected: "slate",
  unavailable: "slate",
  drafting: "slate",
  new: "slate",
};

export function ItemStatusBadge({ status }: { status: string }) {
  const label = status === "awaiting_human" ? "Awaiting review" : status.replace(/_/g, " ");
  return <Badge tone={ITEM_TONE[status] ?? "slate"}>{label.charAt(0).toUpperCase() + label.slice(1)}</Badge>;
}

const PATTERN_TONE: Record<string, Tone> = { rising: "red", recurring: "amber", one_off: "slate" };

export function PatternBadge({ label, theme }: { label: string; theme?: string | null }) {
  return (
    <Badge tone={PATTERN_TONE[label] ?? "slate"} title={theme ? `Matches theme: ${theme}` : undefined}>
      {label === "one_off" ? "One-off" : label.charAt(0).toUpperCase() + label.slice(1)}
    </Badge>
  );
}

/** The source link and its Last checked date, kept on screen (never read aloud). */
export function CitationChip({ citation, lastChecked }: { citation: Citation | null; lastChecked: string | null }) {
  if (!citation) return null;
  const label = citation.title?.trim() || hostname(citation.url);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-600">
      <a
        href={citation.url}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex max-w-full items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-medium text-accent-800 hover:border-accent-600"
        title={citation.url}
      >
        <span aria-hidden>↗</span>
        <span className="truncate">
          {label}
          {citation.page ? `, p. ${citation.page}` : ""}
        </span>
        <span className="shrink-0 text-slate-500">· {hostname(citation.url)}</span>
      </a>
      <span>Last checked {fmtDate(lastChecked ?? citation.last_checked)}</span>
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-sm text-slate-600">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: Error | null | undefined }) {
  if (!error) return null;
  return (
    <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {error.message}
    </p>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <p className="py-8 text-center text-sm text-slate-500">{label}</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="card px-4 py-8 text-center text-sm text-slate-500">{children}</p>;
}

/** A panel that slides in from the right. Escape or the backdrop closes it. */
export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-slate-900/30" onClick={onClose} aria-hidden />
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        className="relative flex h-full w-full max-w-3xl flex-col bg-white shadow-xl outline-none"
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div className="min-w-0 text-sm">{title}</div>
          <button className="btn-secondary px-2 py-1" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  );
}

export function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="mt-6 first:mt-0">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {aside && <div className="text-xs text-slate-500">{aside}</div>}
      </div>
      {children}
    </section>
  );
}

const MODEL_TAGS: Record<string, { label: string; title: string }> = {
  fallback_model: {
    label: "fallback model",
    title: "One model's daily quota ran out, so the other model drafted this",
  },
  fallback: {
    label: "Facts wording",
    title: "The model's wording failed the code checks, so the Facts Agent's answer was used",
  },
};

/** What drafted a reply, when it wasn't the usual path. */
export function ModelTagBadge({ tag }: { tag: string | null }) {
  if (!tag) return null;
  const t = MODEL_TAGS[tag] ?? { label: tag, title: tag };
  return (
    <Badge tone="violet" title={t.title}>
      {t.label}
    </Badge>
  );
}
