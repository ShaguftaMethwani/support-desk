"""Turn spoken numbers and emails into written form, so voice transcripts are checked too.

"nine eight seven six five four three two one zero" -> "9876543210"
"double five" -> "55", "plus nine one ..." -> "+91..."
"rahul dot sharma at gmail dot com" -> "rahul.sharma@gmail.com"

Only runs of four or more spoken digits are converted, so ordinary phrases like
"I have one question about two funds" are left alone.
"""

import re

_DIGITS = {
    "zero": "0", "oh": "0", "o": "0", "nil": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip
_REPEAT = {"double": 2, "triple": 3}
_MIN_RUN_DIGITS = 4

_TOKEN = re.compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]+|\s+")
_SEPARATOR = re.compile(r"^(?:\s+|\s*[-,]\s*)$")
_PLUS = re.compile(r"\bplus\s+(\d{4,})", re.IGNORECASE)

_SPOKEN_EMAIL = re.compile(
    r"\b([a-z0-9]+(?:\s+(?:dot|underscore)\s+[a-z0-9]+)*)\s+(?:at\s+the\s+rate(?:\s+of)?|at)\s+"
    r"([a-z0-9]+(?:\s+dot\s+[a-z0-9]+)*\s+dot\s+(?:com|in|org|net|co|io))\b",
    re.IGNORECASE,
)


def _email_repl(match: re.Match[str]) -> str:
    def fix(part: str) -> str:
        part = re.sub(r"\s+dot\s+", ".", part, flags=re.IGNORECASE)
        return re.sub(r"\s+underscore\s+", "_", part, flags=re.IGNORECASE)

    return f"{fix(match.group(1))}@{fix(match.group(2))}"


def _flush(out: list[str], run: list[str], digits: list[str], has_words: bool) -> None:
    if has_words and len(digits) >= _MIN_RUN_DIGITS:
        out.append("".join(digits))
    else:
        out.extend(run)


def _convert_digit_runs(text: str) -> str:
    tokens = _TOKEN.findall(text)
    out: list[str] = []
    run: list[str] = []  # original tokens in the current run
    digits: list[str] = []
    has_words = False
    repeat = 1
    trailing: list[str] = []  # separators after the last digit, kept if the run ends

    def close() -> None:
        nonlocal run, digits, has_words, repeat, trailing
        if run:
            _flush(out, run, digits, has_words)
        out.extend(trailing)
        run, digits, has_words, repeat, trailing = [], [], False, 1, []

    def extend_run(token: str, value: str) -> None:
        nonlocal trailing, repeat
        run.extend(trailing)
        trailing = []
        run.append(token)
        digits.extend(value * repeat if len(value) == 1 else value)
        repeat = 1

    for token in tokens:
        lower = token.lower()
        if lower in _DIGITS and (lower not in ("oh", "o") or run):
            extend_run(token, _DIGITS[lower])
            has_words = True
        elif token.isdigit():
            if not run:
                close()
            extend_run(token, token)
        elif lower in _REPEAT:
            run.extend(trailing)
            trailing = []
            run.append(token)
            repeat = _REPEAT[lower]
        elif run and _SEPARATOR.match(token):
            trailing.append(token)
        else:
            close()
            out.append(token)
    close()
    # "plus" spoken before a converted number is a country code prefix.
    return _PLUS.sub(r"+\1", "".join(out))


def normalise_spoken(text: str) -> str:
    text = _SPOKEN_EMAIL.sub(_email_repl, text)
    return _convert_digit_runs(text)
