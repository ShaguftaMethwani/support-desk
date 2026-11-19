import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { api } from "../api/client";
import type { QueueListItem } from "../api/types";
import { Badge, Empty, ErrorNote, ItemStatusBadge, Loading, PageHeader, PatternBadge } from "../components/ui";
import { fmtDateTime, groupByMessage, humanize, type MessageGroup } from "../lib/desk";
import { QueueDrawer } from "./QueueDrawer";

const FILTERS = [
  { value: "", label: "All" },
  { value: "awaiting_human", label: "Awaiting review" },
  { value: "blocked", label: "Blocked" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "unavailable", label: "Unavailable" },
];

const CHANNEL_ICON: Record<string, string> = { text: "⌨", voice: "🎙", csv: "★" };

export function Queue() {
  const [params, setParams] = useSearchParams();
  const openId = params.get("item");
  const [status, setStatus] = useState("");
  const [showImport, setShowImport] = useState(false);
  const queue = useQuery({ queryKey: ["queue"], queryFn: () => api.queue(200), refetchInterval: 15_000 });

  const groups = useMemo(() => {
    const items = queue.data?.items ?? [];
    const keep = status ? items.filter((i) => i.status === status) : items;
    return groupByMessage(keep);
  }, [queue.data, status]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of queue.data?.items ?? []) c[i.status] = (c[i.status] ?? 0) + 1;
    return c;
  }, [queue.data]);

  const open = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("item", id);
    else next.delete("item");
    setParams(next, { replace: !id });
  };

  return (
    <div>
      <PageHeader
        title="Queue"
        description="Every question, one row per part, grouped under the message it came in. Open a row to see the draft history, what each agent found, the reviewer's checks and the refinement log."
        actions={
          <button className="btn-secondary" onClick={() => setShowImport((v) => !v)}>
            Import reviews
          </button>
        }
      />
      {showImport && <ImportReviews onDone={() => setShowImport(false)} />}

      <div className="mb-3 flex flex-wrap gap-1" role="group" aria-label="Filter by status">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatus(f.value)}
            aria-pressed={status === f.value}
            className={`rounded-full px-3 py-1 text-sm ${
              status === f.value ? "bg-slate-900 text-white" : "bg-white text-slate-700 ring-1 ring-slate-200 hover:bg-slate-100"
            }`}
          >
            {f.label}
            {f.value && counts[f.value] ? <span className="ml-1 opacity-70">{counts[f.value]}</span> : null}
          </button>
        ))}
      </div>

      <ErrorNote error={queue.error} />
      {queue.isPending ? (
        <Loading />
      ) : groups.length === 0 ? (
        <Empty>No queue items{status ? " with this status" : ""}.</Empty>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[56rem] text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-500 uppercase">
              <tr>
                <th className="px-3 py-2 font-medium">Question</th>
                <th className="px-3 py-2 font-medium">Intent</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Pattern</th>
                <th className="px-3 py-2 font-medium">Flags</th>
                <th className="px-3 py-2 font-medium">Routing</th>
              </tr>
            </thead>
            {groups.map((g) => (
              <GroupRows key={g.messageId} group={g} onOpen={open} />
            ))}
          </table>
        </div>
      )}

      <QueueDrawer itemId={openId} onClose={() => open(null)} />
    </div>
  );
}

function GroupRows({ group, onOpen }: { group: MessageGroup; onOpen: (id: string) => void }) {
  const qc = useQueryClient();
  const multi = group.items.length > 1;
  const approved = group.items.filter((i) => i.status === "approved").length;
  // The API makes one combined email only once every part is approved or rejected.
  const decided = group.items.every((i) => i.status === "approved" || i.status === "rejected");
  const combined = useMutation({
    mutationFn: () => api.combinedReply(group.messageId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["actions"] }),
  });
  return (
    <tbody className="border-b border-slate-200 last:border-b-0">
      <tr className="bg-slate-50/60">
        <td colSpan={6} className="px-3 py-1.5 text-xs text-slate-500">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span title={`Channel: ${group.channel ?? "unknown"}`}>
              {CHANNEL_ICON[group.channel ?? ""] ?? "•"} {group.channel === "csv" ? "review" : group.channel}
            </span>
            <span>{fmtDateTime(group.createdAt)}</span>
            <span>
              Message <code className="text-slate-400">{group.messageId.slice(0, 8)}</code>
              {multi && ` · ${group.items.length} parts`}
            </span>
            {multi && decided && approved > 0 && (
              <span className="ml-auto flex items-center gap-2">
                {combined.isSuccess ? (
                  <Link to="/approvals" className="text-accent-700 hover:underline">
                    Combined email draft created → approvals
                  </Link>
                ) : (
                  <button
                    className="text-accent-700 hover:underline disabled:opacity-50"
                    disabled={combined.isPending}
                    onClick={() => combined.mutate()}
                    title="One email draft holding every approved answer of this message"
                  >
                    Create combined reply ({approved} approved)
                  </button>
                )}
                {combined.isError && <span className="text-red-700">{combined.error.message}</span>}
              </span>
            )}
          </div>
        </td>
      </tr>
      {group.items.map((item) => (
        <ItemRow key={item.id} item={item} multi={multi} onOpen={onOpen} />
      ))}
    </tbody>
  );
}

function ItemRow({ item, multi, onOpen }: { item: QueueListItem; multi: boolean; onOpen: (id: string) => void }) {
  const flags = item.compliance?.incoming_flags ?? [];
  const routing = item.draft?.routing;
  return (
    <tr
      className="cursor-pointer align-top hover:bg-accent-50/50"
      onClick={() => onOpen(item.id)}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onOpen(item.id))}
      tabIndex={0}
      aria-label={`Open: ${item.question}`}
    >
      <td className="max-w-md px-3 py-2">
        <span className={multi ? "pl-3" : ""}>
          {multi && <span className="mr-1 text-slate-400">{item.part_index + 1}.</span>}
          <span className="line-clamp-2">{item.question}</span>
        </span>
        {item.draft?.model_tag && (
          <Badge tone="violet" title="Drafted by the fallback model">
            {item.draft.model_tag}
          </Badge>
        )}
      </td>
      <td className="px-3 py-2 whitespace-nowrap text-slate-700">
        {humanize(item.intent)}
        {item.intent_confidence !== null && (
          <span className="ml-1 text-xs text-slate-400">{Math.round(item.intent_confidence * 100)}%</span>
        )}
      </td>
      <td className="px-3 py-2">
        <ItemStatusBadge status={item.status} />
      </td>
      <td className="px-3 py-2">{item.pattern ? <PatternBadge label={item.pattern.label} theme={item.pattern.theme} /> : "—"}</td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {flags.length === 0 ? (
            <span className="text-slate-400">—</span>
          ) : (
            flags.map((f, i) => (
              <Badge key={i} tone={f.type === "pii" ? "amber" : "red"} title={`From the ${f.source}`}>
                {f.type === "pii" ? `PII: ${f.detail}` : humanize(f.type)}
              </Badge>
            ))
          )}
        </div>
      </td>
      <td className="px-3 py-2 text-xs text-slate-600">
        {routing ? (
          <>
            <span className="font-medium text-slate-800">{humanize(routing.team)}</span> · {routing.priority}
            <br />
            {humanize(routing.action)}
          </>
        ) : (
          "—"
        )}
      </td>
    </tr>
  );
}

function ImportReviews({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [ids, setIds] = useState("");
  const parsed = ids.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const run = useMutation({
    mutationFn: () => api.importReviews(parsed),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["queue"] });
      setIds("");
      onDone();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (parsed.length > 0 && parsed.length <= 20) run.mutate();
  };
  return (
    <form onSubmit={submit} className="card mb-4 p-4">
      <label htmlFor="review-ids" className="text-sm font-medium">
        Review ids to turn into queue items
      </label>
      <p className="text-xs text-slate-500">
        Up to 20, separated by spaces or commas (for example from a theme on the weekly pulse page). Each goes through the
        whole desk, so this takes a while.
      </p>
      <textarea id="review-ids" rows={2} className="input mt-2 font-mono text-xs" value={ids} onChange={(e) => setIds(e.target.value)} />
      <div className="mt-2 flex items-center justify-end gap-3">
        {parsed.length > 20 && <span className="text-xs text-red-700">At most 20 at a time.</span>}
        <ErrorNote error={run.error} />
        <button className="btn-primary" disabled={run.isPending || parsed.length === 0 || parsed.length > 20}>
          {run.isPending ? "Drafting…" : `Import ${parsed.length || ""}`}
        </button>
      </div>
    </form>
  );
}
