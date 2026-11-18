"""Voice input without the network: audio limits, the guard on transcripts, scheme
suggestions, the Groq adapter (with a fake client) and the endpoint."""

import io
import wave
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app.api.auth import current_user
from app.api.queue import spoken_text
from app.api.voice import get_transcriber
from app.config import Settings, get_settings
from app.main import create_app
from app.voice.audio import AudioError, media_type, read_capped, wav_seconds
from app.voice.schemes import suggest_schemes
from app.voice.service import transcribe_clip
from app.voice.transcribe import (
    WHISPER_PROMPT,
    GroqTranscriber,
    Transcription,
    TranscriptionError,
)

SPOKEN = "My PAN is ABCPE1234F and my number is nine eight seven six five four three two one zero"


def wav(seconds: float, rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return out.getvalue()


class FakeTranscriber:
    def __init__(self, text: str = SPOKEN, duration: float | None = 4.0) -> None:
        self.text, self.duration = text, duration
        self.calls: list[tuple[int, str, str]] = []

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcription:
        self.calls.append((len(audio), filename, content_type))
        return Transcription(self.text, self.duration)


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


# --- audio ----------------------------------------------------------------------------------


def test_media_type_accepts_whisper_formats_only() -> None:
    assert media_type("audio/webm;codecs=opus") == "audio/webm"
    assert media_type("AUDIO/WAV") == "audio/wav"
    for bad in ("text/plain", "application/octet-stream", None, ""):
        with pytest.raises(AudioError) as info:
            media_type(bad)
        assert info.value.status == 415


async def test_read_capped_refuses_oversized_and_empty_clips() -> None:
    assert await read_capped(chunks(b"ab", b"cd"), 4) == bytearray(b"abcd")
    with pytest.raises(AudioError) as info:
        await read_capped(chunks(b"ab", b"cde"), 4)
    assert info.value.status == 413
    with pytest.raises(AudioError) as info:
        await read_capped(chunks(), 4)
    assert info.value.status == 422


def test_wav_length_comes_from_the_header() -> None:
    assert wav_seconds(wav(2.5)) == pytest.approx(2.5)
    assert wav_seconds(b"not a wav file at all") is None


# --- transcribe_clip ------------------------------------------------------------------------


async def test_the_transcript_is_redacted_and_the_audio_wiped() -> None:
    buf = bytearray(wav(4.0))
    size = len(buf)
    fake = FakeTranscriber()
    result = await transcribe_clip(buf, "audio/wav", fake, settings())
    assert fake.calls == [(size, "clip.wav", "audio/wav")]
    assert buf == bytearray()  # wiped
    assert result.transcript == ("My PAN is [PAN_REDACTED] and my number is [PHONE_REDACTED]")
    assert result.pii_flags == ["pan", "phone"]
    out = result.as_dict()
    assert "ABCPE1234F" not in str(out) and "9876543210" not in str(out)
    assert out["channel"] == "voice" and out["duration_seconds"] == 4.0


async def test_a_long_wav_is_refused_before_it_is_sent() -> None:
    buf = bytearray(wav(31.0))
    fake = FakeTranscriber()
    with pytest.raises(AudioError) as info:
        await transcribe_clip(buf, "audio/wav", fake, settings())
    assert info.value.status == 413 and fake.calls == [] and buf == bytearray()


async def test_a_long_clip_measured_by_whisper_is_refused() -> None:
    buf = bytearray(b"webm bytes")
    with pytest.raises(AudioError) as info:
        await transcribe_clip(buf, "audio/webm", FakeTranscriber(duration=45.0), settings())
    assert info.value.status == 413 and buf == bytearray()


async def test_silence_is_refused() -> None:
    with pytest.raises(AudioError) as info:
        await transcribe_clip(bytearray(b"x"), "audio/webm", FakeTranscriber(text="  "), settings())
    assert info.value.status == 422


async def test_the_buffer_is_wiped_when_transcription_fails() -> None:
    class Broken:
        async def transcribe(self, audio: bytes, filename: str, content_type: str) -> Any:
            raise TranscriptionError("down")

    buf = bytearray(b"audio")
    with pytest.raises(TranscriptionError):
        await transcribe_clip(buf, "audio/webm", Broken(), settings())
    assert buf == bytearray()


async def test_misheard_scheme_names_come_back_as_suggestions() -> None:
    fake = FakeTranscriber(text="What is the exit load of HDFC big cap fund?")
    result = await transcribe_clip(bytearray(b"x"), "audio/webm", fake, settings())
    assert result.transcript == "What is the exit load of HDFC big cap fund?"  # not changed
    [s] = result.suggestions
    assert (s.heard, s.suggestion, s.scheme) == ("big cap", "Mid Cap", "hdfc_mid_cap")
    assert result.transcript[s.start : s.end] == "big cap"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What is the expense ratio of flexy cap?", [("flexy cap", "Flexi Cap")]),
        ("Is the tax savor fund locked in?", [("tax savor", "Tax Saver")]),
        ("My SIP for the mid cab fund failed", [("mid cab", "Mid Cap")]),
        ("Tell me about the lodge cap fund", [("lodge cap", "Large Cap")]),
        ("What is the exit load of HDFC Mid Cap Fund?", []),  # already right
        ("What about HDFC small cap fund?", []),  # out of scope, not a near-miss
        ("What is the lock in for else?", []),  # single words are never compared
        ("Why is my regular plan charging more than the direct plan?", []),
    ],
)
def test_scheme_suggestions(text: str, expected: list[tuple[str, str]]) -> None:
    found = suggest_schemes(text, min_score=70)
    assert [(s.heard, s.suggestion) for s in found] == expected


# --- Groq adapter ---------------------------------------------------------------------------


class FakeAudio:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def fake_client(outcomes: list[Any]) -> Any:
    audio = FakeAudio(outcomes)
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=audio)), audio


def _status_error(cls: type[openai.APIStatusError], code: int) -> openai.APIStatusError:
    response = httpx.Response(code, request=httpx.Request("POST", "https://api.groq.test"))
    return cls("error", response=response, body=None)


async def test_groq_call_sends_the_scheme_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    client, audio = fake_client([SimpleNamespace(text="hello", duration=3.2)])
    t = GroqTranscriber(settings(groq_api_key="k"), client=client)
    out = await t.transcribe(b"abc", "clip.webm", "audio/webm")
    assert out == Transcription("hello", 3.2)
    [call] = audio.calls
    assert call["model"] == "whisper-large-v3-turbo" and call["language"] == "en"
    assert call["file"] == ("clip.webm", b"abc", "audio/webm")
    assert call["prompt"] == WHISPER_PROMPT
    for name in ("HDFC Mid Cap Fund", "HDFC ELSS Tax Saver", "exit load", "expense ratio"):
        assert name in WHISPER_PROMPT


async def test_groq_rate_limits_are_retried_then_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("app.voice.transcribe.asyncio.sleep", no_sleep)
    limited = _status_error(openai.RateLimitError, 429)
    client, audio = fake_client([limited, SimpleNamespace(text="ok", duration=1.0)])
    t = GroqTranscriber(settings(groq_api_key="k"), client=client)
    assert (await t.transcribe(b"a", "clip.wav", "audio/wav")).text == "ok"
    client, audio = fake_client([limited, limited, limited])
    t = GroqTranscriber(settings(groq_api_key="k"), client=client)
    with pytest.raises(TranscriptionError, match="temporarily unavailable"):
        await t.transcribe(b"a", "clip.wav", "audio/wav")
    assert len(audio.calls) == 3


async def test_a_refused_clip_is_not_retried() -> None:
    client, audio = fake_client([_status_error(openai.BadRequestError, 400)])
    t = GroqTranscriber(settings(groq_api_key="k"), client=client)
    with pytest.raises(TranscriptionError, match="400"):
        await t.transcribe(b"a", "clip.wav", "audio/wav")
    assert len(audio.calls) == 1


# --- endpoint -------------------------------------------------------------------------------


def make_http(transcriber: Any, **overrides: Any) -> TestClient:
    s = settings(app_env="test", weekly_resume_on_startup=False, **overrides)
    app = create_app(s)
    app.dependency_overrides[get_settings] = lambda: s
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id="u", username="t")
    app.dependency_overrides[get_transcriber] = lambda: transcriber
    return TestClient(app)


def test_endpoint_returns_only_the_clean_transcript() -> None:
    fake = FakeTranscriber()
    http = make_http(fake)
    resp = http.post("/chat/voice", content=wav(3.0), headers={"content-type": "audio/wav"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pii_flags"] == ["pan", "phone"] and body["channel"] == "voice"
    assert "ABCPE1234F" not in resp.text and "9876543210" not in resp.text
    assert fake.calls[0][1] == "clip.wav"


def test_endpoint_refusals() -> None:
    fake = FakeTranscriber()
    http = make_http(fake, voice_max_bytes=1000)
    assert http.post("/chat/voice", content=b"hello",
                     headers={"content-type": "text/plain"}).status_code == 415  # fmt: skip
    assert http.post("/chat/voice", content=b"x" * 2000,
                     headers={"content-type": "audio/webm"}).status_code == 413  # fmt: skip
    assert http.post("/chat/voice", content=b"",
                     headers={"content-type": "audio/webm"}).status_code == 422  # fmt: skip
    assert fake.calls == []


def test_endpoint_reports_an_unavailable_transcriber_as_503() -> None:
    class Down:
        async def transcribe(self, audio: bytes, filename: str, content_type: str) -> Any:
            raise TranscriptionError("transcription is temporarily unavailable")

    resp = make_http(Down()).post(
        "/chat/voice", content=b"x", headers={"content-type": "audio/webm"}
    )
    assert resp.status_code == 503


# --- spoken replies -------------------------------------------------------------------------


def test_spoken_text_leaves_the_source_lines_on_screen() -> None:
    reply = (
        "The exit load is 1%.\nSource: https://files.hdfcfund.com/kim.pdf\nLast checked: 2026-11-17"
    )
    assert spoken_text(reply) == "The exit load is 1%."
