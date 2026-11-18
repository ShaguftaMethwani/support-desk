"""A spoken question, from audio to a clean transcript for the user to confirm.

    audio (memory only) -> length checks -> Groq Whisper -> buffer wiped
      -> guard (spoken numbers normalised, PII redacted) -> scheme suggestions

The raw transcript exists only inside this function: it goes straight to the guard and is
never stored, logged or returned. The user confirms (and may edit) the clean transcript,
which then goes to POST /chat/message with channel "voice", the same path as typed text.
"""

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.guard import guard
from app.voice.audio import EXTENSIONS, AudioError, media_type, wav_seconds, wipe
from app.voice.schemes import SchemeSuggestion, suggest_schemes
from app.voice.transcribe import Transcriber

# Whisper's measurement can be a little over the clip's real length.
DURATION_SLACK_SECONDS = 0.5


@dataclass(frozen=True)
class VoiceTranscript:
    transcript: str  # redacted
    pii_flags: list[str]
    suggestions: list[SchemeSuggestion]
    duration_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "transcript": self.transcript,
            "pii_flags": self.pii_flags,
            "suggestions": [s.as_dict() for s in self.suggestions],
            "duration_seconds": self.duration_seconds,
            "channel": "voice",
        }


async def transcribe_clip(
    buf: bytearray, content_type: str | None, transcriber: Transcriber, settings: Settings
) -> VoiceTranscript:
    """Always wipes `buf`, whatever happens."""
    try:
        kind = media_type(content_type)
        limit = settings.voice_max_seconds + DURATION_SLACK_SECONDS
        seconds = wav_seconds(buf) if EXTENSIONS[kind] == "wav" else None
        if seconds is not None and seconds > limit:
            raise AudioError(413, "the clip is longer than 30 seconds")
        heard = await transcriber.transcribe(bytes(buf), f"clip.{EXTENSIONS[kind]}", kind)
    finally:
        wipe(buf)
    duration = heard.duration if heard.duration is not None else seconds
    if duration is not None and duration > limit:
        raise AudioError(413, "the clip is longer than 30 seconds")
    clean = guard(heard.text)
    del heard
    text = " ".join(clean.text.split())
    if not text:
        raise AudioError(422, "no speech was heard in the clip")
    return VoiceTranscript(
        transcript=text,
        pii_flags=clean.pii_flags,
        suggestions=suggest_schemes(text, min_score=settings.voice_scheme_min_score),
        duration_seconds=round(duration, 2) if duration is not None else None,
    )
