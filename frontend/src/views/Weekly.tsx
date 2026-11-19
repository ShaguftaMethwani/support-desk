import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api } from "../api/client";
import type { Theme, WeeklyRun, WeeklyRunSummary } from "../api/types";
import { Badge, Drawer, Empty, ErrorNote, Loading, PageHeader, Section } from "../components/ui";
import { fmtDate, fmtDateTime, humanize } from "../lib/desk";

const ACTIVE = new Set(["pending", "in_progress"]);
const STAGE_ORDER = ["ingest", "cluster", "label", "queue", "quotes", "fees", "explainer", "pulse", "actions"];

const RUN_TONE: Record<string, "green" | "red" | "amber" | "slate" | "accent"> = {
  awaiting_approval: "accent",
  completed: "green",
  failed: "red",
  in_progress: "amber",
  pending: "slate",
};

function RunStatus({ status }: { status: string }) {
  return <Badge tone={RUN_TONE[status] ?? "slate"}>{humanize(status)}</Badge>;
}

export function Weekly() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const runs = useQuery({
    queryKey: ["weekly", "runs"],
    queryFn: api.weeklyRuns,
    refetchInterval: (q) => (q.state.data?.runs.some((r) => ACTIVE.has(r.status)) ? 3000 : 30_000),
  });
  const selected = params.get("run") ?? runs.data?.runs[0]?.run_id ?? null;
  const trigger = useMutation({
    mutationFn: api.triggerWeekly,
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["weekly"] });
      setParams({ run: r.run_id });
    },
  });

  return (
    <div>
      <PageHeader
        title="Weekly pulse"
        description="Each run groups the last 12 weeks of app reviews into themes, adds clusters of similar support questions, checks for fee confusion and writes the pulse. Its doc entry, email draft and explainer promotion wait on the approvals page."
        actions={
          <button className="btn-primary" disabled={trigger.isPending || runs.data?.runs.some((r) => ACTIVE.has(r.status))} onClick={() => trigger.mutate()}>
            {trigger.isPending ? "Starting…" : "Run now"}
          </button>
        }
      />
      <ErrorNote error={trigger.error ?? runs.error} />
      <div className="grid gap-5 lg:grid-cols-[16rem_1fr]">
        <RunList runs={runs.data?.runs} selected={selected} onSelect={(id) => setParams({ run: id })} loading={runs.isPending} />
        <div className="min-w-0">{selected ? <RunDetail runId={selected} /> : !runs.isPending && <Empty>No weekly runs yet. Start one with “Run now”.</Empty>}</div>
      </div>
    </div>
  );
}

function RunList({ runs, selected, onSelect, loading }: { runs?: WeeklyRunSummary[]; selected: string | null; onSelect: (id: string) => void; loading: boolean }) {
  if (loading) return <Loading />;
  return (
    <ul className="card flex max-h-[70vh] flex-col divide-y divide-slate-100 overflow-y-auto" aria-label="Weekly runs">
      {(runs ?? []).map((r) => (
        <li key={r.run_id}>
          <button
            onClick={() => onSelect(r.run_id)}
            aria-current={r.run_id === selected}
            className={`w-full px-3 py-2 text-left text-sm ${r.run_id === selected ? "bg-accent-50" : "hover:bg-slate-50"}`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">{fmtDateTime(r.started_at)}</span>
              <RunStatus status={r.status} />
            </div>
            <p className="text-xs text-slate-500">
              {fmtDate(r.window.start)} – {fmtDate(r.window.end)} · {r.triggered_by}
            </p>
          </button>
        </li>
      ))}
    </ul>
  );
}

type Listing = { title: string; reviewIds: string[]; questionIds: string[] } | null;

function RunDetail({ runId }: { runId: string }) {
  const run = useQuery({
    queryKey: ["weekly", runId],
    queryFn: () => api.weeklyRun(runId),
    refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 3000 : false),
  });
  const [listing, setListing] = useState<Listing>(null);
  if (run.isPending) return <Loading />;
  if (run.error) return <ErrorNote error={run.error} />;
  const r = run.data;
  const reviewThemes = r.themes.filter((t) => !t.from_support_queue);
  const queueThemes = r.themes.filter((t) => t.from_support_queue);

  return (
    <div>
      <div className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-medium">
              Window {fmtDate(r.window.start)} – {fmtDate(r.window.end)}
            </p>
            <p className="text-xs text-slate-500">
              Started {fmtDateTime(r.started_at)} by {r.triggered_by}
              {r.finished_at && ` · finished ${fmtDateTime(r.finished_at)}`}
            </p>
          </div>
          <RunStatus status={r.status} />
        </div>
        <ol className="mt-3 flex flex-wrap gap-1.5" aria-label="Stages">
          {STAGE_ORDER.filter((s) => s in r.stages).map((s) => (
            <li key={s}>
              <Badge tone={r.stages[s] === "done" ? "green" : r.stages[s] === "failed" ? "red" : r.stages[s] === "running" ? "amber" : "slate"}>
                {s}
              </Badge>
            </li>
          ))}
        </ol>
        {r.error && <p className="mt-2 text-sm text-red-700">{r.error}</p>}
        {r.actions.length > 0 && (
          <p className="mt-3 text-sm text-slate-600">
            {r.actions.map((a) => `${humanize(a.kind)} (${a.status})`).join(" · ")} ·{" "}
            <Link to="/approvals" className="text-accent-700 hover:underline">
              Approvals
            </Link>
          </p>
        )}
      </div>

      <Section title="Themes from reviews" aside="click a count to list its reviews">
        {reviewThemes.length === 0 ? (
          <p className="text-sm text-slate-500">No themes yet.</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {reviewThemes.map((t) => (
              <ThemeCard key={t.id} theme={t} onList={setListing} />
            ))}
          </div>
        )}
      </Section>

      {queueThemes.length > 0 && (
        <Section title="From the support queue" aside="clusters of similar customer questions">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {queueThemes.map((t) => (
              <ThemeCard key={t.id} theme={t} onList={setListing} />
            ))}
          </div>
        </Section>
      )}

      {r.quotes.length > 0 && (
        <Section title="What users said">
          <ul className="flex flex-col gap-2">
            {r.quotes.map((q) => (
              <li key={q.review_id} className="card p-3 text-sm">
                <blockquote className="text-slate-800">“{q.text}”</blockquote>
                <p className="mt-1 text-xs text-slate-500">
                  {q.theme} · <code>{q.review_id}</code>
                </p>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {r.pulse && (
        <Section title="Pulse" aside={`${r.pulse.word_count} words`}>
          <pre className="card overflow-x-auto p-4 font-sans text-sm leading-relaxed whitespace-pre-wrap">{r.pulse.text}</pre>
        </Section>
      )}

      <FeeSection run={r} onList={setListing} />

      <ReviewList listing={listing} onClose={() => setListing(null)} />
    </div>
  );
}

function ThemeCard({ theme: t, onList }: { theme: Theme; onList: (l: Listing) => void }) {
  return (
    <div className="card flex flex-col p-4">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium">
          {t.rank && t.rank <= 3 && <span className="mr-1 text-slate-400">#{t.rank}</span>}
          {t.label}
        </h3>
        {t.from_support_queue && <Badge tone="violet">from support queue</Badge>}
      </div>
      {t.description && <p className="mt-1 text-xs text-slate-600">{t.description}</p>}
      <div className="mt-auto flex items-end justify-between gap-2 pt-3">
        <button
          className="text-left text-2xl font-semibold text-accent-800 tabular-nums hover:underline"
          onClick={() => onList({ title: t.label, reviewIds: t.review_ids, questionIds: t.question_ids })}
          title={t.from_support_queue ? "List its queue items" : "List its reviews"}
        >
          {t.count}
          <span className="ml-1 text-xs font-normal text-slate-500">{t.from_support_queue ? "questions" : "reviews"}</span>
        </button>
        <span className="text-xs text-slate-500">
          {fmtDate(t.window.start)} – {fmtDate(t.window.end)}
        </span>
      </div>
    </div>
  );
}

function FeeSection({ run: r, onList }: { run: WeeklyRun; onList: (l: Listing) => void }) {
  const counts = r.fees?.counts ?? [];
  if (!r.explainer && counts.length === 0) return null;
  return (
    <Section title="Fees">
      {r.fees?.label && (
        <p className="mb-2 text-sm">
          Fee confusion found: <span className="font-medium">{r.fees.label}</span>
        </p>
      )}
      {counts.length > 0 && (
        <ul className="mb-3 flex flex-wrap gap-2 text-sm">
          {counts.map((c) => (
            <li key={c.fee_type}>
              <button className="card px-3 py-1.5 hover:border-accent-600" onClick={() => onList({ title: c.label, reviewIds: c.review_ids, questionIds: [] })}>
                {c.label} <span className="font-semibold tabular-nums">{c.reviews}</span>
                {!c.mutual_fund && <span className="ml-1 text-xs text-slate-500">(not MF)</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      {r.explainer && (
        <div className="card p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-medium">Fee explainer · {humanize(r.explainer.fee_type)}</h3>
            <span className="text-xs text-slate-500">Last checked {fmtDate(r.explainer.last_checked)}</span>
          </div>
          <ul className="mt-2 list-disc pl-5 text-sm">
            {r.explainer.bullets.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
          <ul className="mt-2 text-xs break-all">
            {r.explainer.source_urls.map((u) => (
              <li key={u}>
                <a className="text-accent-700 hover:underline" href={u} target="_blank" rel="noopener noreferrer">
                  {u}
                </a>
              </li>
            ))}
          </ul>
          {r.explainer.evidence_review_ids.length > 0 && (
            <button className="mt-2 text-xs text-accent-700 hover:underline" onClick={() => onList({ title: "Fee explainer evidence", reviewIds: r.explainer!.evidence_review_ids, questionIds: [] })}>
              {r.explainer.evidence_review_ids.length} evidence reviews
            </button>
          )}
        </div>
      )}
    </Section>
  );
}

const PAGE = 50;

function ReviewList({ listing, onClose }: { listing: Listing; onClose: () => void }) {
  const [shown, setShown] = useState(PAGE);
  const ids = listing?.reviewIds.slice(0, shown) ?? [];
  // One request per page of 50 ids (the API takes at most 200 at a time).
  const pages = useQueries({
    queries: Array.from({ length: Math.ceil(ids.length / PAGE) }, (_, i) => {
      const page = ids.slice(i * PAGE, (i + 1) * PAGE);
      return { queryKey: ["reviews", page], queryFn: () => api.reviews(page), staleTime: Infinity };
    }),
  });
  const reviews = { error: pages.find((p) => p.error)?.error ?? null, isFetching: pages.some((p) => p.isFetching) };
  const byId = new Map(pages.flatMap((p) => p.data?.reviews ?? []).map((r) => [r.review_id, r]));
  return (
    <Drawer
      open={!!listing}
      onClose={() => {
        setShown(PAGE);
        onClose();
      }}
      title={
        <>
          <p className="font-medium">{listing?.title}</p>
          <p className="text-xs text-slate-500">
            {listing?.reviewIds.length ? `${listing.reviewIds.length} review ids` : `${listing?.questionIds.length ?? 0} queue items`}
          </p>
        </>
      }
    >
      {listing && listing.questionIds.length > 0 && (
        <ul className="flex flex-col gap-1 text-sm">
          {listing.questionIds.map((id) => (
            <li key={id}>
              <Link to={`/queue?item=${id}`} className="font-mono text-xs text-accent-700 hover:underline">
                {id}
              </Link>
            </li>
          ))}
        </ul>
      )}
      <ErrorNote error={reviews.error} />
      <ul className="flex flex-col divide-y divide-slate-100">
        {ids.map((id) => {
          const r = byId.get(id);
          return (
            <li key={id} className="py-2 text-sm">
              <p className="font-mono text-xs text-slate-500">
                {id}
                {r && ` · ${fmtDate(r.date)}${r.rating ? ` · ${r.rating}★` : ""}`}
              </p>
              {r ? <p className="mt-0.5 text-slate-800">{r.text}</p> : reviews.isFetching ? <p className="text-xs text-slate-400">…</p> : null}
            </li>
          );
        })}
      </ul>
      {listing && listing.reviewIds.length > shown && (
        <button className="btn-secondary mt-3" onClick={() => setShown((s) => s + PAGE)}>
          Show {Math.min(PAGE, listing.reviewIds.length - shown)} more
        </button>
      )}
    </Drawer>
  );
}
