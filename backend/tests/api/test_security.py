import uuid

from app.security import hash_password, make_session_token, read_session_token, verify_password

SECRET = b"s" * 32


def test_password_round_trip() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$") and "correct" not in stored
    assert verify_password("correct horse battery", stored)
    assert not verify_password("correct horse batterY", stored)
    # Same password, different salt.
    assert hash_password("correct horse battery") != stored


def test_unknown_or_malformed_hashes_never_match() -> None:
    assert not verify_password("anything", None)
    assert not verify_password("anything", "!")  # the local developer user
    assert not verify_password("anything", "scrypt$x$y$z$zz$zz")


def test_session_token_round_trip_and_expiry() -> None:
    uid = uuid.uuid4()
    token = make_session_token(uid, SECRET, now=1000)
    assert read_session_token(token, SECRET, max_age=60, now=1030) == uid
    assert read_session_token(token, SECRET, max_age=60, now=1061) is None
    assert read_session_token(token, SECRET, max_age=60, now=999) is None  # issued in the future


def test_session_token_rejects_tampering() -> None:
    uid = uuid.uuid4()
    token = make_session_token(uid, SECRET, now=1000)
    other = make_session_token(uuid.uuid4(), SECRET, now=1000)
    forged = f"{other.split('.')[0]}.{token.split('.', 1)[1]}"
    assert read_session_token(forged, SECRET, max_age=60, now=1000) is None
    assert read_session_token(token, b"t" * 32, max_age=60, now=1000) is None
    assert read_session_token("garbage", SECRET, max_age=60, now=1000) is None
    assert read_session_token(token + "0", SECRET, max_age=60, now=1000) is None


def test_login_limiter_counts_failures_per_username_in_a_window() -> None:
    from app.api.auth import LoginLimiter

    now = [0.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    for _ in range(5):
        assert limiter.retry_after("grader", 5, 900) is None
        limiter.failed("grader")
    assert limiter.retry_after("grader", 5, 900) == 900
    assert limiter.retry_after("someone-else", 5, 900) is None
    now[0] = 600
    assert limiter.retry_after("grader", 5, 900) == 300
    now[0] = 901  # the first failure has left the window
    assert limiter.retry_after("grader", 5, 900) is None
    limiter.failed("grader")
    limiter.succeeded("grader")  # a successful sign-in clears the count
    assert limiter.retry_after("grader", 1, 900) is None
