import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api/client";
import type { ChatResponse, QueueItem, SchemeSuggestion, VoiceResponse } from "../api/types";
import { AnswerBadge, CitationChip, ErrorNote, PageHeader } from "../components/ui";
import { answerStatus, applySuggestion, humanize, replyBody, spokenScript } from "../lib/desk";
import { MAX_SECONDS, useRecorder } from "../lib/useRecorder";

const EXAMPLES = [
  "What is the exit load of HDFC Liquid Fund?",
  "What is the expense ratio of HDFC Mid Cap Fund, and should I invest in it?",
  "How do I download my capital gains statement?",
];

interface Exchange {
  id: string;
  text: string;
  channel: "text" | "voice";
  response: ChatResponse;
}

const SESSION_KEY = "desk.chatSession";

function readSession(): string | null {
  try {
    return sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

export function Chat() {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(readSession);
  const [voice, setVoice] = useState<{ text: string; pii: string[]; suggestions: SchemeSuggestion[] } | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const send = useMutation({
    mutationFn: (v: { text: string; channel: "text" | "voice" }) =>
      api.sendMessage({ text: v.text, channel: v.channel, session_id: sessionId }),
    onSuccess: (response, v) => {
      setExchanges((xs) => [...xs, { id: response.message_id, text: v.text, channel: v.channel, response }]);
      setSessionId(response.session_id);
      try {
        sessionStorage.setItem(SESSION_KEY, response.session_id);
      } catch {
        /* the session simply isn't remembered across reloads */
      }
      if (v.channel === "text") setText("");
      else setVoice(null);
      qc.invalidateQueries({ queryKey: ["queue"] });
    },
  });

  const transcribe = useMutation({
    mutationFn: api.transcribe,
    onSuccess: (r: VoiceResponse) => setVoice({ text: r.transcript, pii: r.pii_flags, suggestions: r.suggestions }),
  });
  const recorder = useRecorder((clip) => transcribe.mutate(clip));

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [exchanges.length, send.isPending]);

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (t && !send.isPending) send.mutate({ text: t, channel: "text" });
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const newConversation = () => {
    setExchanges([]);
    setSessionId(null);
    try {
      sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* nothing stored */
    }
  };

  const busy = send.isPending || transcribe.isPending;

  return (
    <div className="mx-auto flex max-w-3xl flex-col">
      <PageHeader
        title="Chat"
        description="Ask about HDFC Mutual Fund schemes: fees, exit loads, statements, how things work. Each question gets its own answer and source. Answers are drafts for the agent; nothing reaches a customer from here."
        actions={
          exchanges.length > 0 && (
            <button className="btn-secondary" onClick={newConversation}>
              New conversation
            </button>
          )
        }
      />

      {exchanges.length === 0 && !send.isPending && (
        <div className="card p-5">
          <p className="text-sm text-slate-700">
            Hello. I answer factual questions from official HDFC Mutual Fund, SEBI and AMFI documents, with a
            source for every answer. I don't give investment advice. Try one of these:
          </p>
          <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            {EXAMPLES.map((q) => (
              <button
                key={q}
                className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-left text-sm text-slate-700 hover:border-accent-600 hover:text-accent-800"
                onClick={() => send.mutate({ text: q, channel: "text" })}
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-6">
        {exchanges.map((x) => (
          <ExchangeView key={x.id} exchange={x} />
        ))}
        {send.isPending && (
          <div className="flex flex-col gap-2">
            <UserBubble text={send.variables.text} channel={send.variables.channel} />
            <p className="text-sm text-slate-500">Checking the documents… the three agents run in parallel, this takes a few seconds.</p>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-0 mt-6 bg-slate-50 pb-2 pt-2">
        <ErrorNote error={send.error ?? transcribe.error} />
        {recorder.error && <p className="mb-2 text-sm text-red-700">{recorder.error}</p>}

        {voice ? (
          <TranscriptConfirm
            voice={voice}
            onChange={setVoice}
            onCancel={() => setVoice(null)}
            onConfirm={() => voice.text.trim() && send.mutate({ text: voice.text.trim(), channel: "voice" })}
            busy={send.isPending}
          />
        ) : (
          <form onSubmit={submit} className="card mt-2 flex items-end gap-2 p-2 shadow-sm">
            <label htmlFor="chat-input" className="sr-only">
              Your question
            </label>
            <textarea
              id="chat-input"
              rows={2}
              className="min-h-[2.5rem] flex-1 resize-none border-0 px-2 py-1.5 text-sm focus:outline-none"
              placeholder="Type a question, or use the microphone"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onKeyDown}
              maxLength={4000}
              disabled={busy}
            />
            {recorder.state !== "unsupported" &&
              (recorder.state === "recording" ? (
                <button type="button" className="btn-danger" onClick={recorder.stop}>
                  <span className="inline-block size-2 animate-pulse rounded-full bg-red-600" aria-hidden />
                  Stop {MAX_SECONDS - recorder.seconds}s
                </button>
              ) : (
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={recorder.start}
                  disabled={busy}
                  title={`Record a question (up to ${MAX_SECONDS} seconds)`}
                >
                  {transcribe.isPending ? "Transcribing…" : "🎙 Speak"}
                </button>
              ))}
            <button type="submit" className="btn-primary" disabled={busy || !text.trim()}>
              Send
            </button>
          </form>
        )}
      </div>
    </div>
  );
}

function UserBubble({ text, channel }: { text: string; channel: "text" | "voice" }) {
  return (
    <div className="ml-auto max-w-[85%] rounded-lg rounded-br-sm bg-accent-700 px-3 py-2 text-sm whitespace-pre-wrap text-white">
      {channel === "voice" && (
        <span className="mb-0.5 block text-xs text-accent-100" aria-label="Spoken">
          🎙 spoken
        </span>
      )}
      {text}
    </div>
  );
}

function ExchangeView({ exchange }: { exchange: Exchange }) {
  const { answers, dropped } = exchange.response;
  return (
    <div className="flex flex-col gap-2">
      <UserBubble text={exchange.text} channel={exchange.channel} />
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-slate-500">
          {answers.length > 1 ? `${answers.length} questions found in this message` : "1 question"}
        </p>
        <PlayButton answers={answers} />
      </div>
      {answers.map((a, i) => (
        <AnswerBlock key={a.id} item={a} index={answers.length > 1 ? i + 1 : null} />
      ))}
      {dropped && (
        <div className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-600">
          <p className="font-medium text-slate-800">{dropped.message}</p>
          <ul className="mt-1 list-disc pl-5">
            {dropped.questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function AnswerBlock({ item, index }: { item: QueueItem; index: number | null }) {
  const status = answerStatus(item);
  const d = item.draft;
  return (
    <article className="card p-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-slate-800">
          {index !== null && <span className="mr-1 text-slate-500">{index}.</span>}
          {item.question}
        </p>
        <div className="flex items-center gap-1.5">
          {item.intent && <span className="text-xs text-slate-500">{humanize(item.intent)}</span>}
          <AnswerBadge status={status} />
        </div>
      </header>
      {status === "unavailable" ? (
        <p className="mt-2 text-sm text-slate-600">
          The answer is temporarily unavailable (the language model could not be reached). It stays in the queue and
          nothing was sent.
        </p>
      ) : d ? (
        <>
          <p className="mt-2 text-sm leading-relaxed whitespace-pre-wrap text-slate-800">{replyBody(d.reply)}</p>
          {status === "blocked" && (
            <p className="mt-2 text-xs text-red-700">
              Blocked by the reviewer. It can't be approved until an agent edits it in the queue.
            </p>
          )}
          <div className="mt-3">
            <CitationChip citation={d.citation} lastChecked={d.last_checked} />
          </div>
        </>
      ) : null}
    </article>
  );
}

/** Reads every answer of the message aloud, each prefixed with its question. The source and
 * Last checked date stay on screen and are not read. */
function PlayButton({ answers }: { answers: QueueItem[] }) {
  const [speaking, setSpeaking] = useState(false);
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;
  useEffect(() => () => {
    if (speaking) window.speechSynthesis.cancel();
  }, [speaking]);
  if (!supported) return null;
  const readable = answers.filter((a) => a.draft && answerStatus(a) !== "unavailable");
  if (readable.length === 0) return null;

  const play = () => {
    const synth = window.speechSynthesis;
    synth.cancel();
    const lines = spokenScript(readable.map((a) => ({ question: a.question, spoken: a.draft!.spoken_text })));
    lines.forEach((line, i) => {
      const u = new SpeechSynthesisUtterance(line);
      u.lang = "en-IN";
      if (i === lines.length - 1) u.onend = u.onerror = () => setSpeaking(false);
      synth.speak(u);
    });
    setSpeaking(true);
  };
  const stop = () => {
    window.speechSynthesis.cancel();
    setSpeaking(false);
  };
  return speaking ? (
    <button className="btn-secondary px-2 py-1 text-xs" onClick={stop}>
      ■ Stop
    </button>
  ) : (
    <button className="btn-secondary px-2 py-1 text-xs" onClick={play} title="Read the answers aloud">
      ▶ Play reply
    </button>
  );
}

function TranscriptConfirm({
  voice,
  onChange,
  onCancel,
  onConfirm,
  busy,
}: {
  voice: { text: string; pii: string[]; suggestions: SchemeSuggestion[] };
  onChange: (v: { text: string; pii: string[]; suggestions: SchemeSuggestion[] }) => void;
  onCancel: () => void;
  onConfirm: () => void;
  busy: boolean;
}) {
  return (
    <div className="card mt-2 border-accent-600 p-3 shadow-sm">
      <label htmlFor="transcript" className="text-sm font-medium text-slate-800">
        Check the transcript before sending
      </label>
      <p className="text-xs text-slate-500">Fix any misheard words. The audio was not kept.</p>
      <textarea
        id="transcript"
        rows={3}
        className="input mt-2"
        value={voice.text}
        onChange={(e) => onChange({ ...voice, text: e.target.value })}
        maxLength={4000}
      />
      {voice.pii.length > 0 && (
        <p className="mt-2 text-xs text-amber-800">
          Removed personal details: {voice.pii.map(humanize).join(", ")}. They were never shown or stored.
        </p>
      )}
      {voice.suggestions.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-slate-600">Did you mean:</span>
          {voice.suggestions.map((s, i) => (
            <button
              key={`${s.start}-${s.suggestion}`}
              className="rounded-full border border-accent-600 bg-accent-50 px-2.5 py-1 text-accent-800 hover:bg-accent-100"
              onClick={() => onChange({ ...voice, ...applySuggestion(voice.text, voice.suggestions, i) })}
              title={s.scheme_name}
            >
              “{s.heard}” → {s.suggestion}
            </button>
          ))}
        </div>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onCancel} disabled={busy}>
          Discard
        </button>
        <button className="btn-primary" onClick={onConfirm} disabled={busy || !voice.text.trim()}>
          {busy ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
