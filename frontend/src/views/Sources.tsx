import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { Source } from "../api/types";
import { Badge, Empty, ErrorNote, ItemStatusBadge, Loading, PageHeader } from "../components/ui";
import { fmtDate, humanize } from "../lib/desk";

const PUBLISHER: Record<string, string> = {
  hdfc_mf: "HDFC Mutual Fund",
  sebi: "SEBI",
  amfi: "AMFI",
  indmoney_help: "INDMoney help",
};

const DOC_TYPE: Record<string, string> = {
  kim: "Key Information Memorandum",
  sid: "Scheme Information Document",
  sai: "Statement of Additional Information",
  factsheet: "Factsheet",
  fees: "Total expense ratios",
  faq: "FAQ",
  statement_guide: "Statement guide",
  regulation: "Regulation",
  education: "Investor education",
  platform_process: "Platform help",
};

const SCHEME: Record<string, string> = {
  hdfc_large_cap: "Large Cap",
  hdfc_flexi_cap: "Flexi Cap",
  hdfc_elss_tax_saver: "ELSS Tax Saver",
  hdfc_mid_cap: "Mid Cap",
  hdfc_liquid: "Liquid",
};

/** A readable name: stored titles are sometimes a PDF's first line ("CIRCULAR"). */
function sourceName(s: Source): string {
  const type = DOC_TYPE[s.doc_type] ?? humanize(s.doc_type);
  const schemes = s.schemes.filter((x) => x !== "general").map((x) => SCHEME[x] ?? humanize(x));
  if (s.publisher === "hdfc_mf") return schemes.length === 1 ? `HDFC ${schemes[0]} Fund · ${type}` : `HDFC MF · ${type}`;
  const fallback = `${PUBLISHER[s.publisher] ?? s.publisher} · ${type}`;
  const title = s.title?.split("|")[0]?.replace(/^[:\s]+/, "").trim() ?? "";
  const allCaps = /^[A-Z\s]+$/.test(title);
  return title.length > 12 && !allCaps ? title : fallback;
}

export function Sources() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const [filter, setFilter] = useState("");
  const list = useMemo(() => {
    const all = sources.data?.sources ?? [];
    return filter ? all.filter((s) => s.publisher === filter) : all;
  }, [sources.data, filter]);
  const publishers = [...new Set((sources.data?.sources ?? []).map((s) => s.publisher))];

  return (
    <div>
      <PageHeader
        title="Sources"
        description="The official public documents every answer is drawn from. Each answer cites one of them, with the date it was last fetched and checked."
      />
      <div className="mb-3 flex flex-wrap gap-1" role="group" aria-label="Filter by publisher">
        {["", ...publishers].map((p) => (
          <button
            key={p}
            aria-pressed={filter === p}
            onClick={() => setFilter(p)}
            className={`rounded-full px-3 py-1 text-sm ${filter === p ? "bg-slate-900 text-white" : "bg-white text-slate-700 ring-1 ring-slate-200 hover:bg-slate-100"}`}
          >
            {p ? PUBLISHER[p] ?? p : "All"}
          </button>
        ))}
      </div>
      <ErrorNote error={sources.error} />
      {sources.isPending ? (
        <Loading />
      ) : list.length === 0 ? (
        <Empty>No sources ingested yet.</Empty>
      ) : (
        <ul className="flex flex-col gap-3">
          {list.map((s) => (
            <SourceRow key={s.id} source={s} />
          ))}
        </ul>
      )}
    </div>
  );
}

function SourceRow({ source: s }: { source: Source }) {
  return (
    <li className="card p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <a href={s.url} target="_blank" rel="noopener noreferrer" className="font-medium text-accent-800 hover:underline">
            {sourceName(s)} ↗
          </a>
          <p className="mt-0.5 text-xs text-slate-500">
            {PUBLISHER[s.publisher] ?? s.publisher} · <code>{s.key}</code> · {s.chunks} passages
          </p>
        </div>
        <div className="text-right text-xs text-slate-600">
          <p>Last checked {fmtDate(s.last_checked)}</p>
          <p className="mt-0.5">
            <Badge tone={s.citing_count ? "accent" : "slate"}>
              cited by {s.citing_count} draft{s.citing_count === 1 ? "" : "s"}
            </Badge>
          </p>
        </div>
      </div>
      {s.citing.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-slate-500">Drafts that cite it</summary>
          <ul className="mt-2 flex flex-col gap-1.5">
            {s.citing.map((c) => (
              <li key={c.draft_id} className="flex flex-wrap items-center gap-2 text-sm">
                <Link to={`/queue?item=${c.item_id}`} className="min-w-0 flex-1 truncate text-slate-800 hover:text-accent-800 hover:underline">
                  {c.question}
                </Link>
                <span className="text-xs text-slate-500">
                  v{c.version}
                  {c.page ? ` · p. ${c.page}` : ""}
                </span>
                <Badge>{humanize(c.source_type)}</Badge>
                <ItemStatusBadge status={c.item_status} />
              </li>
            ))}
            {s.citing_count > s.citing.length && <li className="text-xs text-slate-500">and {s.citing_count - s.citing.length} older drafts</li>}
          </ul>
        </details>
      )}
    </li>
  );
}
