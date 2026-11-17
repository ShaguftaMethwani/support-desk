"""Database write guard: refuse to store PII in any `*_redacted` column.

Text should already be clean, because guard() runs at ingress. This is the backstop that makes
"no PII stored anywhere" hold even if a code path forgets to call guard().

Covered: ORM unit-of-work inserts and updates (session.add, attribute changes) and ORM-enabled
insert()/update() statements run through a Session. Not covered: raw Core statements run on a
Connection. User text must therefore always be written through the ORM.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Mapper, ORMExecuteState, Session

from app.guard.redactor import find_pii

REDACTED_SUFFIX = "_redacted"


class UnredactedPIIError(ValueError):
    """Raised instead of writing PII to the database. The message never includes the value."""


def check_value(table: str, column: str, value: object) -> None:
    if isinstance(value, str):
        found = find_pii(value, names=False, normalise=False)
        if found:
            types = ", ".join(t.value for t in found)
            raise UnredactedPIIError(f"Refusing to store PII ({types}) in {table}.{column}")


def _check_instance(mapper: Mapper[Any], _connection: Any, target: Any) -> None:
    for column in mapper.columns:
        if column.key.endswith(REDACTED_SUFFIX):
            table = getattr(mapper.local_table, "name", "?")
            check_value(table, column.key, getattr(target, column.key, None))


def _check_rows(table: str, rows: Iterable[Mapping[str, Any]]) -> None:
    for row in rows:
        for key, value in row.items():
            if str(key).endswith(REDACTED_SUFFIX):
                check_value(table, str(key), value)


def _check_orm_statement(state: ORMExecuteState) -> None:
    if not (state.is_insert or state.is_update):
        return
    statement: Any = state.statement
    table = getattr(getattr(statement, "table", None), "name", "?")
    params = state.parameters
    if isinstance(params, Mapping):
        _check_rows(table, [params])
    elif params:
        _check_rows(table, params)
    try:
        compiled_params: Mapping[str, Any] | None = statement.compile().params
    except Exception:  # noqa: BLE001 - multi-row VALUES may not compile without a dialect
        compiled_params = None
    if compiled_params is not None:
        _check_rows(table, [compiled_params])
    else:
        for group in getattr(statement, "_multi_values", ()):
            _check_rows(table, [{getattr(k, "key", k): v for k, v in row.items()} for row in group])


_installed = False


def install_db_write_guard(base: type) -> None:
    global _installed
    if _installed:
        return
    event.listen(base, "before_insert", _check_instance, propagate=True)
    event.listen(base, "before_update", _check_instance, propagate=True)
    event.listen(Session, "do_orm_execute", _check_orm_statement)
    _installed = True
