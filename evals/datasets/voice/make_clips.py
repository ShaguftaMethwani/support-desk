"""Generate the voice test clips with macOS text-to-speech (`say` and `afconvert`).

    python evals/datasets/voice/make_clips.py

Each line of clips.jsonl gives the voice, the format and the words (`say`, or `text` when the
spoken and expected words are the same). WAV clips are 16 kHz mono; m4a clips are AAC. The
PII clips use made-up values: a PAN, a bank account number and a phone number that belong to
no one.

These are synthetic voices, clearer than a phone microphone in a noisy room. Real recordings
made in the browser are a better test and can be added to clips.jsonl the same way.
"""

import json
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
CLIPS = HERE / "clips"


def main() -> None:
    CLIPS.mkdir(exist_ok=True)
    for line in (HERE / "clips.jsonl").read_text().splitlines():
        clip = json.loads(line)
        words = clip.get("say", clip["text"])
        out = CLIPS / f"{clip['id']}.{clip['format']}"
        with tempfile.TemporaryDirectory() as tmp:
            aiff = Path(tmp) / "clip.aiff"
            subprocess.run(["say", "-v", clip["voice"], "-r", "175", "-o", str(aiff), words],
                           check=True)  # fmt: skip
            if clip["format"] == "wav":
                args = ["-f", "WAVE", "-d", "LEI16@16000", "-c", "1"]
            else:
                args = ["-f", "m4af", "-d", "aac", "-c", "1"]
            subprocess.run(["afconvert", *args, str(aiff), str(out)], check=True)
        print(f"{out.name}  {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
