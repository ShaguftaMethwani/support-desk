import { useCallback, useEffect, useRef, useState } from "react";

export const MAX_SECONDS = 30;

// In order of preference. Safari records audio/mp4; the API accepts all of these.
const TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

function pickType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  return TYPES.find((t) => MediaRecorder.isTypeSupported(t));
}

export type RecorderState = "idle" | "recording" | "unsupported";

/** Records one clip from the microphone, stopping by itself at 30 seconds. The audio stays
 * in memory and is handed to `onClip`; nothing is saved by the browser. */
export function useRecorder(onClip: (clip: Blob) => void) {
  const supported = typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";
  const [state, setState] = useState<RecorderState>(supported ? "idle" : "unsupported");
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const timer = useRef<number | null>(null);
  const onClipRef = useRef(onClip);
  onClipRef.current = onClip;

  const cleanup = useCallback(() => {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
    recorder.current?.stream.getTracks().forEach((t) => t.stop());
    recorder.current = null;
  }, []);

  const stop = useCallback(() => {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }, []);

  const start = useCallback(async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const type = pickType();
      const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => e.data.size > 0 && chunks.push(e.data);
      rec.onstop = () => {
        const clip = new Blob(chunks, { type: rec.mimeType || type || "audio/webm" });
        chunks.length = 0;
        cleanup();
        setState("idle");
        if (clip.size > 0) onClipRef.current(clip);
      };
      recorder.current = rec;
      rec.start();
      const began = Date.now();
      setSeconds(0);
      setState("recording");
      timer.current = window.setInterval(() => {
        const s = Math.floor((Date.now() - began) / 1000);
        setSeconds(s);
        // Stop just under the cap, so the clip is never refused for length.
        if (Date.now() - began >= (MAX_SECONDS - 0.5) * 1000) stop();
      }, 250);
    } catch (e) {
      cleanup();
      setState("idle");
      setError(e instanceof DOMException && e.name === "NotAllowedError" ? "Microphone access was blocked." : "Could not start the microphone.");
    }
  }, [cleanup, stop]);

  useEffect(() => cleanup, [cleanup]);

  return { state, seconds, error, start, stop };
}
