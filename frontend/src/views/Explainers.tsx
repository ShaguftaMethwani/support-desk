import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { CanonicalExplainer } from "../api/types";
import { Badge, Empty, ErrorNote, Loading, PageHeader, Section } from "../components/ui";
import { fmtDate, fmtDateTime, humanize } from "../lib/desk";

export function Explainers() {
  const [archived, setArchived] = useState(false);
  const explainers = useQuery({
    queryKey: ["explainers", archived],
    queryFn: () => api.canonicalExplainers(archived),
  });
  const actions = useQuery({ queryKey: ["actions"], queryFn: api.pendingActions });
  const promotions = (actions.data?.actions ?? []).filter((a) => a.kind === "promote_canonical");

  return (
    <div>
      <PageHeader
        title="Canonical explainers"
        description="Fee explainers from the weekly loop that a person promoted. Related customer questions are answered with their bullets, word for word, and each reuse is counted."
        actions={
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} />
            Show replaced ones
          </label>
        }
      />

      {promotions.length > 0 && (
        <div className="mb-5 rounded-lg border border-accent-100 bg-accent-50 p-4">
          <p className="text-sm font-medium text-accent-800">
            {promotions.length} promotion{promotions.length === 1 ? "" : "s"} waiting for approval
          </p>
          <ul className="mt-2 flex flex-col gap-1 text-sm">
            {promotions.map((p) => (
              <li key={p.id} className="flex flex-wrap items-center gap-2">
                <span>{humanize(String(p.payload.fee_type ?? ""))}</span>
                <span className="text-xs text-slate-500">from a weekly run, {fmtDateTime(p.created_at)}</span>
                {p.status === "failed" && <Badge tone="red">failed</Badge>}
                <Link to="/approvals" className="text-accent-700 hover:underline">
                  Review on the approvals page →
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}

      <ErrorNote error={explainers.error} />
      {explainers.isPending ? (
        <Loading />
      ) : !explainers.data ? null : explainers.data.length === 0 ? (
        <Empty>No canonical explainer is active yet. Approve a weekly run's “Promote to canonical explainer” action to add one.</Empty>
      ) : (
        <Section title={archived ? "All explainers" : "Active"}>
          <div className="flex flex-col gap-3">
            {explainers.data.map((e) => (
              <ExplainerCard key={e.id} explainer={e} />
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function ExplainerCard({ explainer: e }: { explainer: CanonicalExplainer }) {
  return (
    <article className={`card p-4 ${e.active ? "" : "opacity-70"}`}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-medium">{e.label}</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Approved {fmtDateTime(e.approved_at)}
            {e.approved_by && ` by ${e.approved_by}`} · from{" "}
            <Link to={`/weekly?run=${e.weekly_run_id}`} className="text-accent-700 hover:underline">
              weekly run
            </Link>{" "}
            · Last checked {fmtDate(e.last_checked)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {e.active ? <Badge tone="green">active</Badge> : <Badge>replaced {fmtDate(e.archived_at)}</Badge>}
          <div className="text-right">
            <p className="text-2xl leading-none font-semibold text-accent-800 tabular-nums">{e.reuse_count}</p>
            <p className="text-xs text-slate-500">reuses</p>
          </div>
        </div>
      </header>
      <ul className="mt-3 list-disc pl-5 text-sm">
        {e.bullets.map((b, i) => (
          <li key={i}>{b}</li>
        ))}
      </ul>
      <ul className="mt-3 text-xs break-all">
        {e.source_urls.map((u) => (
          <li key={u}>
            <a href={u} target="_blank" rel="noopener noreferrer" className="text-accent-700 hover:underline">
              {u}
            </a>
          </li>
        ))}
      </ul>
    </article>
  );
}
