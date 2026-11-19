"""Create a desk user, or set a new password for an existing one.

Run from backend/:
    uv run python -m scripts.create_user <username> [--role agent|admin]

The password is asked for twice on the terminal, or read from the DESK_PASSWORD environment
variable when set (for scripted setup). Only its scrypt hash is stored.
"""

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from app.db.models import AppUser
from app.db.session import get_engine, get_sessionmaker
from app.security import hash_password

MIN_LENGTH = 10


def read_password() -> str:
    if env := os.environ.get("DESK_PASSWORD"):
        return env
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Again: "):
        sys.exit("Passwords do not match.")
    return first


async def upsert(username: str, password: str, role: str) -> str:
    async with get_sessionmaker()() as s:
        user = await s.scalar(select(AppUser).where(AppUser.username == username))
        if user is None:
            s.add(AppUser(username=username, password_hash=hash_password(password), role=role))
            outcome = "created"
        else:
            user.password_hash = hash_password(password)
            user.role = role
            outcome = "updated"
        await s.commit()
    await get_engine().dispose()
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("username")
    parser.add_argument("--role", choices=["agent", "admin"], default="agent")
    args = parser.parse_args()
    if args.username == "local-dev":
        sys.exit("local-dev is reserved for local requests without a login.")
    password = read_password()
    if len(password) < MIN_LENGTH:
        sys.exit(f"Use at least {MIN_LENGTH} characters.")
    outcome = asyncio.run(upsert(args.username, password, args.role))
    print(f"User {args.username} {outcome}.")


if __name__ == "__main__":
    main()
