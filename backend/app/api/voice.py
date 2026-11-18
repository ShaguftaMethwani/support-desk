"""Voice input (architecture section 11).

POST /chat/voice   raw audio body (webm, ogg, wav, mp3, m4a or flac; at most 30 seconds),
                   Content-Type set to the audio type. Returns the redacted transcript, the
                   PII types removed and any scheme-name suggestions. Nothing is stored: the
                   confirmed transcript goes to POST /chat/message with "channel": "voice".

The body is read as a stream into memory, never through a temporary file, and request bodies
are never logged (see the request middleware in app/main.py).
"""

from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import current_user
from app.config import Settings, get_settings
from app.db.models import AppUser
from app.voice.audio import AudioError, read_capped
from app.voice.service import transcribe_clip
from app.voice.transcribe import GroqTranscriber, Transcriber, TranscriptionError

router = APIRouter(tags=["voice"])


@lru_cache
def _groq_transcriber() -> GroqTranscriber:
    return GroqTranscriber(get_settings())


def get_transcriber() -> Transcriber:
    return _groq_transcriber()


@router.post("/chat/voice")
async def chat_voice(
    request: Request,
    _user: Annotated[AppUser, Depends(current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    transcriber: Annotated[Transcriber, Depends(get_transcriber)],
) -> dict[str, Any]:
    try:
        buf = await read_capped(request.stream(), settings.voice_max_bytes)
        result = await transcribe_clip(
            buf, request.headers.get("content-type"), transcriber, settings
        )
    except AudioError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except TranscriptionError as exc:
        raise HTTPException(503, str(exc)) from None
    return result.as_dict()
