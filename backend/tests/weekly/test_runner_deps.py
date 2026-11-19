"""The weekly runner shares the desk's dependencies: one embedding model per process."""

from typing import Any

import pytest

from app.config import Settings
from app.corpus.embed import HashEmbedder
from app.orchestrator import deps as desk_deps
from app.orchestrator.desk import DeskDeps
from app.weekly.runner import default_deps


def test_weekly_runner_reuses_the_desk_model_and_client(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = DeskDeps(
        sessions=object(),  # type: ignore[arg-type]
        llm=object(),  # type: ignore[arg-type]
        embedder=HashEmbedder(),
        retriever_for=lambda _session: None,  # type: ignore[arg-type,return-value]
        settings=Settings(),
    )
    monkeypatch.setattr(desk_deps, "get_desk_deps", lambda: desk)

    weekly: Any = default_deps()

    assert weekly.embedder is desk.embedder
    assert weekly.llm is desk.llm
    assert weekly.sessions is desk.sessions
    assert weekly.settings is desk.settings
