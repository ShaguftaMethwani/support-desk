"""Reading an audio clip into memory, within its limits.

The clip is read from the request stream into a bytearray: never into a file, and never
through FastAPI's UploadFile, whose temporary file spills to disk above 1 MB. The caller wipes
the buffer once Whisper has it.
"""

import io
import wave
from collections.abc import AsyncIterator

# Groq Whisper accepts flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav and webm.
EXTENSIONS: dict[str, str] = {
    "audio/webm": "webm",
    "video/webm": "webm",  # some browsers label MediaRecorder audio this way
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}


# Enough for any WAV header, including the padding chunks some encoders add before the data.
WAV_HEADER_BYTES = 65536


class AudioError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def media_type(header: str | None) -> str:
    """'audio/webm;codecs=opus' -> 'audio/webm'. Refuses anything Whisper can't read."""
    kind = (header or "").split(";", 1)[0].strip().lower()
    if kind not in EXTENSIONS:
        raise AudioError(415, "send the clip as webm, ogg, wav, mp3, m4a or flac audio")
    return kind


async def read_capped(chunks: AsyncIterator[bytes], max_bytes: int) -> bytearray:
    buf = bytearray()
    async for chunk in chunks:
        if len(buf) + len(chunk) > max_bytes:
            wipe(buf)
            raise AudioError(413, "the clip is too large; keep it under 30 seconds")
        buf.extend(chunk)
    if not buf:
        raise AudioError(422, "the clip is empty")
    return buf


def wipe(buf: bytearray) -> None:
    """Overwrite, then empty, so the audio doesn't linger in freed memory."""
    buf[:] = bytes(len(buf))
    buf.clear()


def wav_seconds(data: bytes | bytearray) -> float | None:
    """A WAV clip's length from its header, before it is sent anywhere. None for other formats
    (Whisper's own measurement is checked afterwards)."""
    # The header is enough: the frame count comes from the data chunk's declared size.
    header = io.BytesIO(bytes(data[:WAV_HEADER_BYTES]))
    try:
        with wave.open(header) as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else None
    except (wave.Error, EOFError):
        return None
    finally:
        header.close()
