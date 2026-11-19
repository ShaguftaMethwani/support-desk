import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "../api/client";
import type { PendingAction } from "../api/types";
import { Badge, Empty, ErrorNote, Loading, PageHeader } from "../components/ui";
import { fmtDateTime, humanize } from "../lib/desk";

const KIND: Record<string, { label: string; tool: string }> = {
  doc_append: { label: "Append to the Google Doc", tool: "google_docs_append" },
  email_draft: { label: "Create a Gmail draft", tool: "gmail_create_draft" },
  promote_canonical: { label: "Promote to canonical explainer", tool: "internal, no external write" },
};

function targetLabel(a: PendingAction): string {
  if (a.target === "gmail_drafts") return "Gmail drafts (never sent)";
  if (a.target === "internal") return "Canonical explainers, used in customer replies";
  if (a.kind === "doc_append") return `Google Doc ${a.target}`;
  return a.target;
}

function OriginLink({ a }: { a: PendingAction }) {
  if (a.origin === "queue_item") return <Link className="text-accent-700 hover:underline" to={`/queue?item=${a.origin_id}`}>queue item</Link>;
  if (a.origin === "weekly_run") return <Link className="text-accent-700 hover:underline" to={`/weekly?run=${a.origin_id}`}>weekly run</Link>;
  if (a.origin === "message") return <span>multi-part message (combined reply)</span>;
  return <span>{a.origin}</span>;
}

export function Approvals() {
  const actions = useQuery({ queryKey: ["actions"], queryFn: api.pendingActions, refetchInterval: 15_000 });
  const list = actions.data?.actions ?? [];
  return (
    <div>
      <PageHeader
        title="Approvals"
        description="Nothing is written to Google until it is approved here, one action at a time. Approving sends the exact payload shown on the card: if it changed since this page loaded, the server refuses."
      />
      <ErrorNote error={actions.error} />
      {actions.isPending ? (
        <Loading />
      ) : list.length === 0 ? (
        <Empty>No actions are waiting. Approving a draft in the queue, or a weekly run, adds them here.</Empty>
      ) : (
        <div className="flex flex-col gap-4">
          {list.map((a) => (
            <ActionCard key={a.id} action={a} />
          ))}
        </div>
      )}
    </div>
  );
}

function ActionCard({ action: a }: { action: PendingAction }) {
  const qc = useQueryClient();
  const done = () => {
    qc.invalidateQueries({ queryKey: ["actions"] });
    qc.invalidateQueries({ queryKey: ["explainers"] });
    qc.invalidateQueries({ queryKey: ["weekly"] });
  };
  const approve = useMutation({ mutationFn: () => api.approveAction(a.id, a.payload_hash), onSuccess: done });
  const reject = useMutation({ mutationFn: () => api.rejectAction(a.id), onSuccess: done });
  const retry = useMutation({ mutationFn: () => api.retryAction(a.id), onSuccess: done });
  const busy = approve.isPending || reject.isPending || retry.isPending;
  const kind = KIND[a.kind] ?? { label: humanize(a.kind), tool: a.kind };

  return (
    <article className={`card min-w-0 p-4 [overflow-wrap:anywhere] ${a.status === "failed" ? "border-red-300" : ""}`} aria-label={kind.label}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="font-medium">{kind.label}</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Target: <span className="text-slate-700">{targetLabel(a)}</span> · Tool: <code>{kind.tool}</code> · From a{" "}
            <OriginLink a={a} /> · {fmtDateTime(a.created_at)}
          </p>
        </div>
        <Badge tone={a.status === "failed" ? "red" : "accent"}>{a.status}</Badge>
      </header>

      <div className="mt-3 min-w-0 overflow-x-auto rounded-md border border-slate-200 bg-slate-50 p-3">
        <PayloadPreview action={a} />
      </div>
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-slate-500">Raw payload · hash {a.payload_hash.slice(0, 12)}…</summary>
        <pre className="mt-1 max-h-80 overflow-auto rounded bg-slate-900 p-3 text-slate-100">{JSON.stringify(a.payload, null, 2)}</pre>
      </details>

      {a.status === "failed" && a.error && <p className="mt-3 text-sm text-red-700">Failed: {a.error}</p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {a.status === "failed" ? (
          <button className="btn-primary" disabled={busy} onClick={() => retry.mutate()} title="Runs again under the approval already given">
            {retry.isPending ? "Retrying…" : "Retry"}
          </button>
        ) : (
          <>
            <button className="btn-primary" disabled={busy} onClick={() => approve.mutate()}>
              {approve.isPending ? "Running…" : "Approve"}
            </button>
            <button className="btn-danger" disabled={busy} onClick={() => reject.mutate()}>
              Reject
            </button>
          </>
        )}
        <ErrorNote error={approve.error ?? reject.error ?? retry.error} />
      </div>
    </article>
  );
}

function PayloadPreview({ action: a }: { action: PendingAction }) {
  const p = a.payload;
  if (a.kind === "email_draft") {
    return (
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-slate-500">To</dt>
        <dd>{typeof p.to === "string" && p.to ? p.to : <span className="text-slate-500">no recipient (draft only)</span>}</dd>
        <dt className="text-slate-500">Subject</dt>
        <dd>{String(p.subject ?? "")}</dd>
        <dt className="text-slate-500">Body</dt>
        <dd className="whitespace-pre-wrap">{String(p.body ?? "")}</dd>
      </dl>
    );
  }
  if (a.kind === "promote_canonical") {
    const bullets = (p.bullets as string[] | undefined) ?? [];
    const sources = (p.source_urls as string[] | undefined) ?? [];
    return (
      <div className="text-sm">
        <p className="text-xs text-slate-500">
          Fee type: {humanize(String(p.fee_type ?? ""))} {p.last_checked ? `· Last checked ${String(p.last_checked)}` : ""}
        </p>
        <ul className="mt-2 list-disc pl-5">
          {bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
        <ul className="mt-2 text-xs break-all">
          {sources.map((u) => (
            <li key={u}>
              <a className="text-accent-700 hover:underline" href={u} target="_blank" rel="noopener noreferrer">
                {u}
              </a>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-amber-800">
          Once approved, related customer questions are answered with these bullets, word for word.
        </p>
      </div>
    );
  }
  return <Fields value={p} />;
}

/** Every field of a payload, nested values included, so nothing in it is hidden. */
function Fields({ value }: { value: Record<string, unknown> }) {
  return (
    <dl className="grid grid-cols-1 gap-y-2 text-sm sm:grid-cols-[10rem_1fr] sm:gap-x-3">
      {Object.entries(value).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate-500">{humanize(k)}</dt>
          <dd className="min-w-0">
            {v === null || v === undefined ? (
              <span className="text-slate-400">—</span>
            ) : typeof v === "object" ? (
              <pre className="overflow-x-auto text-xs whitespace-pre-wrap">{JSON.stringify(v, null, 2)}</pre>
            ) : (
              <span className="whitespace-pre-wrap">{String(v)}</span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}
