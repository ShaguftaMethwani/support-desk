import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { AgentRun, QueueItemDetail, RuleResult } from "../api/types";
import { AnswerBadge, Badge, CitationChip, Drawer, ErrorNote, ItemStatusBadge, Loading, ModelTagBadge, PatternBadge, Section } from "../components/ui";
import { answerStatus, fmtDate, fmtDateTime, humanize, replyBody, timingRounds } from "../lib/desk";

const MAX_ROUNDS = 5;
const REFINE_PRESETS = ["Make it shorter", "Friendlier tone", "Check against a past theme"];

export function QueueDrawer({ itemId, onClose }: { itemId: string | null; onClose: () => void }) {
  const detail = useQuery({
    queryKey: ["queue", itemId],
    queryFn: () => api.queueItem(itemId!),
    enabled: !!itemId,
  });
  const item = detail.data;
  return (
    <Drawer
      open={!!itemId}
      onClose={onClose}
      title={
        item ? (
          <>
            <p className="font-medium text-slate-900">{item.question}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-slate-500">
              <ItemStatusBadge status={item.status} />
              <AnswerBadge status={answerStatus(item)} />
              <span>
                {humanize(item.intent)}
                {item.intent_confidence !== null && ` (${Math.round(item.intent_confidence * 100)}%)`}
              </span>
              <span>· {item.channel === "csv" ? "review" : item.channel}</span>
              <span>· {fmtDateTime(item.created_at)}</span>
            </div>
          </>
        ) : (
          "Queue item"
        )
      }
    >
      <ErrorNote error={detail.error} />
      {detail.isPending ? <Loading /> : item ? <DrawerBody item={item} /> : null}
    </Drawer>
  );
}

function DrawerBody({ item }: { item: QueueItemDetail }) {
  const d = item.draft;
  const slots = Object.entries(item.slots).filter(([k, v]) => v && !k.endsWith("_inherited") && k !== "intent_method");
  return (
    <>
      {slots.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1.5 text-xs">
          {slots.map(([k, v]) => (
            <Badge key={k}>
              {humanize(k)}: {String(v)}
            </Badge>
          ))}
          {Boolean(item.slots.scheme_inherited || item.slots.fee_inherited) && (
            <Badge tone="blue" title="Carried over from an earlier part or message in the session">
              shared slot
            </Badge>
          )}
        </div>
      )}

      {d && (
        <Section title={`Current draft · v${d.version}`} aside={d.author === "human" ? "edited by a person" : "drafted by the desk"}>
          <div className={`card p-4 ${d.review_status === "blocked" ? "border-red-300" : ""}`}>
            <p className="text-sm leading-relaxed whitespace-pre-wrap">{replyBody(d.reply)}</p>
            <div className="mt-3">
              <CitationChip citation={d.citation} lastChecked={d.last_checked} />
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5 text-xs">
              <Badge>{humanize(d.source_type)}</Badge>
              {d.canonical_explainer_id && (
                <Link to="/explainers" className="hover:underline">
                  <Badge tone="accent">canonical explainer</Badge>
                </Link>
              )}
              <ModelTagBadge tag={d.model_tag} />
              <Badge tone={d.review_status === "passed" ? "green" : "red"}>review {d.review_status}</Badge>
            </div>
          </div>
          {d.routing && (
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-slate-500">Route to</dt>
              <dd>
                {humanize(d.routing.team)} · {d.routing.priority} · {humanize(d.routing.action)}
              </dd>
              <dt className="text-slate-500">Why</dt>
              <dd>{d.routing.reason}</dd>
              {d.routing.disagreement && (
                <>
                  <dt className="text-amber-700">Agents disagree</dt>
                  <dd className="text-amber-900">{d.routing.disagreement}</dd>
                </>
              )}
            </dl>
          )}
        </Section>
      )}

      <Controls item={item} />

      <Section title="Agent timing" aside="Facts, Pattern and Compliance start together">
        <Timing runs={item.agent_runs} versions={item.versions.map((v) => ({ id: v.id, version: v.version }))} />
      </Section>

      <Section title="Agent outputs" aside="the latest run of each">
        <AgentOutputs runs={item.agent_runs} />
      </Section>

      <Section title="Draft versions and reviewer results">
        <ol className="flex flex-col gap-3">
          {[...item.versions].reverse().map((v) => (
            <li key={v.id} className="card p-3">
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span className="font-medium text-slate-800">v{v.version}</span>
                <span>{v.author === "human" ? "edited by a person" : "system"}</span>
                <Badge tone={v.review_status === "passed" ? "green" : v.review_status === "rework" ? "amber" : "red"}>
                  {v.review_status}
                </Badge>
                <ModelTagBadge tag={v.model_tag} />
              </div>
              <p className="mt-2 text-sm whitespace-pre-wrap text-slate-700">{replyBody(v.reply)}</p>
              {v.review && <Rules rules={v.review} />}
            </li>
          ))}
        </ol>
      </Section>

      <Section title="Refinement log" aside={`${item.refinements.length} of ${MAX_ROUNDS} rounds used`}>
        {item.refinements.length === 0 ? (
          <p className="text-sm text-slate-500">No refinements yet.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-slate-500">
              <tr>
                <th className="py-1 font-medium">Round</th>
                <th className="py-1 font-medium">Feedback type</th>
                <th className="py-1 font-medium">Agents re-run</th>
              </tr>
            </thead>
            <tbody>
              {item.refinements.map((r) => (
                <tr key={r.round} className="border-t border-slate-100">
                  <td className="py-1">{r.round}</td>
                  <td className="py-1">{humanize(r.feedback_type)}</td>
                  <td className="py-1">{r.agents_fired.length ? r.agents_fired.join(", ") : "none (orchestrator and reviewer only)"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );
}

function Rules({ rules }: { rules: RuleResult[] }) {
  return (
    <ul className="mt-2 flex flex-wrap gap-1.5">
      {rules.map((r) => (
        <li key={r.rule}>
          <Badge tone={r.passed ? "green" : "red"} title={r.detail ?? undefined}>
            {r.passed ? "✓" : "✕"} {r.rule}
            {!r.passed && r.detail ? `: ${r.detail}` : ""}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

type Mode = null | "edit" | "refine";

function Controls({ item }: { item: QueueItemDetail }) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<Mode>(null);
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<ReactNode>(null);
  useEffect(() => {
    setMode(null);
    setNotice(null);
  }, [item.id]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["queue"] });
    qc.invalidateQueries({ queryKey: ["actions"] });
  };
  const approve = useMutation({
    mutationFn: () => api.approveItem(item.id),
    onSuccess: (r) => {
      refresh();
      setNotice(
        <>
          Approved. {r.pending_action_ids.length} pending action{r.pending_action_ids.length === 1 ? "" : "s"} created, each
          needing its own approval.{" "}
          <Link to="/approvals" className="font-medium underline">
            Go to approvals
          </Link>
        </>,
      );
    },
  });
  const reject = useMutation({ mutationFn: () => api.rejectItem(item.id), onSuccess: refresh });
  const edit = useMutation({
    mutationFn: (reply: string) => api.editDraft(item.id, reply),
    onSuccess: (r) => {
      refresh();
      setMode(null);
      setNotice(r.draft.review_status === "passed" ? "Saved as a new version. It passed review." : "Saved as a new version, but it failed review: it can't be approved. See the reviewer results below.");
    },
  });
  const refine = useMutation({
    mutationFn: (feedback: string) => api.refine(item.id, feedback),
    onSuccess: (r) => {
      refresh();
      setMode(null);
      setNotice(`Round ${r.round}: read as “${humanize(r.feedback_type)}”. Re-ran ${r.agents_fired.length ? r.agents_fired.join(", ") + ", " : ""}orchestrator and reviewer.`);
    },
  });

  const d = item.draft;
  const open = item.status === "awaiting_human" || item.status === "blocked";
  const busy = approve.isPending || reject.isPending || edit.isPending || refine.isPending;
  const roundsLeft = MAX_ROUNDS - item.refinements.length;
  const approveBlockedReason = !d
    ? "There is no draft"
    : !d.approvable
      ? "The current draft failed review. Edit it so it passes, then approve."
      : !open
        ? `This item is already ${item.status}`
        : undefined;

  const startEdit = () => {
    setText(d?.reply ?? "");
    setMode("edit");
  };
  const startRefine = () => {
    setText("");
    setMode("refine");
  };

  return (
    <Section title="Decision">
      {open ? (
        <div className="flex flex-wrap gap-2">
          <button
            className="btn-primary"
            disabled={busy || !!approveBlockedReason}
            onClick={() => approve.mutate()}
            title={approveBlockedReason ?? "Accept this draft. Creates the doc entry and email draft actions; nothing is written yet."}
          >
            Approve draft
          </button>
          <button className="btn-secondary" disabled={busy || !d} onClick={startEdit}>
            Edit
          </button>
          <button className="btn-secondary" disabled={busy || !d || roundsLeft <= 0} onClick={startRefine} title={roundsLeft <= 0 ? "All 5 refinement rounds are used" : undefined}>
            Refine ({roundsLeft} left)
          </button>
          <button className="btn-danger" disabled={busy} onClick={() => reject.mutate()}>
            Reject
          </button>
        </div>
      ) : (
        <p className="text-sm text-slate-600">This item is {item.status}. No further decisions.</p>
      )}
      {open && approveBlockedReason && d && <p className="mt-2 text-xs text-red-700">{approveBlockedReason}</p>}

      {mode === "edit" && (
        <div className="mt-3">
          <label htmlFor="edit-reply" className="text-xs font-medium text-slate-600">
            Edit the reply (keep the Source and Last checked lines). It is checked again by the reviewer.
          </label>
          <textarea id="edit-reply" rows={6} className="input mt-1 font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} />
          <div className="mt-2 flex justify-end gap-2">
            <button className="btn-secondary" onClick={() => setMode(null)}>
              Cancel
            </button>
            <button className="btn-primary" disabled={edit.isPending || !text.trim()} onClick={() => edit.mutate(text)}>
              {edit.isPending ? "Checking…" : "Save as new version"}
            </button>
          </div>
        </div>
      )}
      {mode === "refine" && (
        <div className="mt-3">
          <label htmlFor="refine" className="text-xs font-medium text-slate-600">
            Feedback for the desk. Only the agents it concerns run again.
          </label>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {REFINE_PRESETS.map((p) => (
              <button key={p} className="rounded-full border border-slate-300 px-2.5 py-0.5 text-xs hover:border-accent-600" onClick={() => setText(p)}>
                {p}
              </button>
            ))}
          </div>
          <input id="refine" className="input mt-2" value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. The customer meant HDFC Flexi Cap Fund" maxLength={2000} />
          <div className="mt-2 flex justify-end gap-2">
            <button className="btn-secondary" onClick={() => setMode(null)}>
              Cancel
            </button>
            <button className="btn-primary" disabled={refine.isPending || !text.trim()} onClick={() => refine.mutate(text)}>
              {refine.isPending ? "Refining…" : "Refine"}
            </button>
          </div>
        </div>
      )}
      <div className="mt-2">
        <ErrorNote error={approve.error ?? reject.error ?? edit.error ?? refine.error} />
        {notice && <p className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900">{notice}</p>}
      </div>
    </Section>
  );
}

const AGENT_COLOR: Record<string, string> = {
  facts: "bg-sky-500",
  pattern: "bg-amber-500",
  compliance: "bg-violet-500",
};

function Timing({ runs, versions }: { runs: AgentRun[]; versions: { id: string; version: number }[] }) {
  const rounds = timingRounds(runs);
  if (rounds.length === 0) return <p className="text-sm text-slate-500">No agent runs recorded.</p>;
  const versionOf = (id: string | null) => versions.find((v) => v.id === id)?.version;
  return (
    <div className="flex flex-col gap-4">
      {rounds.map((r, i) => (
        <div key={r.draftVersionId ?? i}>
          <p className="mb-1 text-xs text-slate-500">
            {versionOf(r.draftVersionId) ? `For v${versionOf(r.draftVersionId)}` : "Run"} · {(r.totalMs / 1000).toFixed(1)} s wall time
          </p>
          <div className="flex flex-col gap-1">
            {r.bars.map((b) => (
              <div key={b.agent} className="grid grid-cols-[6rem_1fr_4rem] items-center gap-2 text-xs">
                <span className="text-slate-700">{humanize(b.agent)}</span>
                <div className="relative h-3 rounded bg-slate-100">
                  <div
                    className={`absolute top-0 h-3 rounded ${AGENT_COLOR[b.agent] ?? "bg-slate-500"} ${b.status === "error" ? "opacity-40" : ""}`}
                    style={{ left: `${(b.startMs / r.totalMs) * 100}%`, width: `max(3px, ${(b.durationMs / r.totalMs) * 100}%)` }}
                  />
                </div>
                <span className="text-right text-slate-500 tabular-nums">
                  {b.status === "cached" ? "cached" : `${(b.durationMs / 1000).toFixed(1)} s`}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function latest(runs: AgentRun[], agent: string): AgentRun | undefined {
  return [...runs].reverse().find((r) => r.agent === agent);
}

function AgentOutputs({ runs }: { runs: AgentRun[] }) {
  const facts = latest(runs, "facts");
  const pattern = latest(runs, "pattern");
  const compliance = latest(runs, "compliance");
  return (
    <div className="grid gap-3 lg:grid-cols-3">
      <AgentCard title="Facts" run={facts}>
        {facts?.output && <FactsOutput output={facts.output} />}
      </AgentCard>
      <AgentCard title="Pattern" run={pattern}>
        {pattern?.output && <PatternOutput output={pattern.output} />}
      </AgentCard>
      <AgentCard title="Compliance" run={compliance}>
        {compliance?.output && <ComplianceOutputView output={compliance.output} />}
      </AgentCard>
    </div>
  );
}

function AgentCard({ title, run, children }: { title: string; run?: AgentRun; children: ReactNode }) {
  return (
    <div className="card p-3 text-xs">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-medium">{title}</span>
        {run ? <Badge tone={run.status === "error" ? "red" : run.status === "cached" ? "slate" : "green"}>{run.status}</Badge> : <Badge>not run</Badge>}
      </div>
      {run?.status === "error" && <p className="text-red-700">The agent failed; the item shows as unavailable.</p>}
      {children}
    </div>
  );
}

function FactsOutput({ output }: { output: Record<string, unknown> }) {
  const searched = (output.searched as { source: string; page: number | null; similarity: number | null }[] | undefined) ?? [];
  return (
    <div className="flex flex-col gap-1.5 text-slate-700">
      <p>
        <span className="text-slate-500">Status:</span> {humanize(String(output.status ?? ""))}
      </p>
      {typeof output.answer === "string" && <p className="leading-relaxed">{output.answer}</p>}
      {typeof output.reason === "string" && <p className="text-slate-600">{output.reason}</p>}
      {searched.length > 0 && (
        <details>
          <summary className="cursor-pointer text-slate-500">Searched {searched.length} passages</summary>
          <ul className="mt-1 flex flex-col gap-0.5">
            {searched.map((s, i) => (
              <li key={i} className="font-mono">
                {s.source}
                {s.page ? ` p.${s.page}` : ""} {s.similarity !== null ? `· ${s.similarity.toFixed(2)}` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function PatternOutput({ output }: { output: Record<string, unknown> }) {
  const evidence = (output.evidence_ids as string[] | undefined) ?? [];
  return (
    <div className="flex flex-col gap-1.5 text-slate-700">
      <div>
        <PatternBadge label={String(output.label)} />
      </div>
      <p>
        {String(output.review_count)} similar reviews, {String(output.question_count)} similar questions,{" "}
        {fmtDate(output.window_start as string)} to {fmtDate(output.window_end as string)}.
      </p>
      <p>
        This week {String(output.this_week)}, previous week {String(output.previous_week)}.
      </p>
      {typeof output.matched_theme_label === "string" && (
        <p>
          <span className="text-slate-500">Theme:</span> {output.matched_theme_label}
          {typeof output.theme_similarity === "number" && ` (${output.theme_similarity.toFixed(2)})`}
        </p>
      )}
      {evidence.length > 0 && (
        <details>
          <summary className="cursor-pointer text-slate-500">{evidence.length} evidence ids</summary>
          <ul className="mt-1 font-mono break-all">
            {evidence.map((id) => (
              <li key={id}>{id}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function ComplianceOutputView({ output }: { output: Record<string, unknown> }) {
  const flags = (output.incoming_flags as { type: string; detail: string; source: string }[] | undefined) ?? [];
  const risk = String(output.risk_level);
  return (
    <div className="flex flex-col gap-1.5 text-slate-700">
      <div>
        <Badge tone={risk === "high" ? "red" : risk === "medium" ? "amber" : "green"}>{risk} risk</Badge>
      </div>
      {flags.length > 0 && (
        <ul>
          {flags.map((f, i) => (
            <li key={i}>
              {humanize(f.type)}: {f.detail} <span className="text-slate-400">({f.source})</span>
            </li>
          ))}
        </ul>
      )}
      {typeof output.notes === "string" && <p className="text-slate-600">{output.notes}</p>}
    </div>
  );
}
