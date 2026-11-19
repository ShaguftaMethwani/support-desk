import { describe, expect, it } from "vitest";
import type { AgentRun, Draft, QueueListItem, SchemeSuggestion } from "../api/types";
import { answerStatus, applySuggestion, groupByMessage, replyBody, spokenScript, timingRounds } from "./desk";

const draft = (over: Partial<Draft> = {}): Draft => ({
  id: "d",
  version: 1,
  reply: "x",
  spoken_text: "x",
  citation: null,
  last_checked: null,
  source_type: "corpus",
  canonical_explainer_id: null,
  routing: null,
  author: "system",
  model_tag: null,
  review_status: "passed",
  approvable: true,
  ...over,
});

describe("answerStatus", () => {
  it("maps drafts to the four answer states", () => {
    expect(answerStatus({ status: "awaiting_human", draft: draft() })).toBe("answered");
    expect(answerStatus({ status: "awaiting_human", draft: draft({ source_type: "canonical_explainer" }) })).toBe("answered");
    expect(answerStatus({ status: "awaiting_human", draft: draft({ source_type: "refusal" }) })).toBe("refused");
    expect(answerStatus({ status: "awaiting_human", draft: draft({ source_type: "not_covered" }) })).toBe("not_covered");
    expect(answerStatus({ status: "blocked", draft: draft({ review_status: "blocked" }) })).toBe("blocked");
  });

  it("blocked wins over refusal, and model failures show as unavailable", () => {
    expect(answerStatus({ status: "blocked", draft: draft({ source_type: "refusal" }) })).toBe("blocked");
    expect(answerStatus({ status: "unavailable", draft: null })).toBe("unavailable");
  });
});

it("replyBody drops the source lines", () => {
  expect(replyBody("Exit load is 1%.\nSource: https://x\nLast checked: 2026-11-18")).toBe("Exit load is 1%.");
});

it("groupByMessage keeps parts in order under the newest message first", () => {
  const item = (message_id: string, part_index: number, created_at: string) =>
    ({ message_id, part_index, created_at, channel: "text" }) as QueueListItem;
  const groups = groupByMessage([item("a", 1, "2026-11-18T10:00"), item("b", 0, "2026-11-18T11:00"), item("a", 0, "2026-11-18T10:00")]);
  expect(groups.map((g) => g.messageId)).toEqual(["b", "a"]);
  expect(groups[1]!.items.map((i) => i.part_index)).toEqual([0, 1]);
});

it("timingRounds shows the agents overlapping within a round", () => {
  const run = (agent: string, start: string, end: string, draft = "v1"): AgentRun => ({
    agent,
    status: "ok",
    started_at: `2026-11-18T08:34:${start}Z`,
    finished_at: `2026-11-18T08:34:${end}Z`,
    latency_ms: null,
    draft_version_id: draft,
    output: null,
  });
  const [round] = timingRounds([run("facts", "51.000", "58.000"), run("pattern", "51.100", "55.000"), run("compliance", "51.100", "51.100")]);
  expect(round!.totalMs).toBe(7000);
  expect(round!.bars.map((b) => [b.agent, b.startMs, b.durationMs])).toEqual([
    ["facts", 0, 7000],
    ["pattern", 100, 3900],
    ["compliance", 100, 0],
  ]);
});

describe("applySuggestion", () => {
  const s = (heard: string, start: number, suggestion: string): SchemeSuggestion => ({
    heard,
    start,
    end: start + heard.length,
    suggestion,
    scheme: "x",
    scheme_name: "x",
    score: 80,
  });

  it("replaces the words and shifts later suggestions", () => {
    const text = "exit load of big cap and flexy cap";
    const out = applySuggestion(text, [s("big cap", 13, "Mid Cap"), s("flexy cap", 25, "Flexi Cap")], 0);
    expect(out.text).toBe("exit load of Mid Cap and flexy cap");
    expect(out.suggestions).toHaveLength(1);
    const again = applySuggestion(out.text, out.suggestions, 0);
    expect(again.text).toBe("exit load of Mid Cap and Flexi Cap");
  });

  it("drops a suggestion whose words were edited away", () => {
    const out = applySuggestion("exit load of mid cap", [s("big cap", 13, "Mid Cap")], 0);
    expect(out).toEqual({ text: "exit load of mid cap", suggestions: [] });
  });
});

it("spokenScript prefixes each answer with its question", () => {
  expect(spokenScript([{ question: "What is the exit load?", spoken: "It is 1%." }])).toEqual([
    "You asked: What is the exit load. It is 1%.",
  ]);
  expect(spokenScript([{ question: "A?", spoken: "a" }, { question: "B", spoken: "b" }])[1]).toBe("Question 2: B. b");
});
