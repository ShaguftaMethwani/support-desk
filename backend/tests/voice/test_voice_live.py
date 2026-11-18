"""Real Groq Whisper on a recorded clip. Skipped in CI. Run with: uv run pytest -m llm"""

from pathlib import Path

import pytest

from app.config import Settings
from app.voice.service import transcribe_clip
from app.voice.transcribe import GroqTranscriber

pytestmark = pytest.mark.llm
CLIPS = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "voice" / "clips"


async def test_a_spoken_pan_comes_back_redacted() -> None:
    settings = Settings()
    if settings.groq_api_key is None:
        pytest.skip("GROQ_API_KEY not set")
    buf = bytearray((CLIPS / "p01.wav").read_bytes())
    result = await transcribe_clip(buf, "audio/wav", GroqTranscriber(settings), settings)
    assert result.pii_flags == ["pan"] and "[PAN_REDACTED]" in result.transcript
    assert "1234" not in result.transcript and buf == bytearray()
