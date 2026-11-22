"""Give Yolt's database user a new random password and rewrite .env and .db-password.

    uv run python dev/rotate_db_password.py

Connects with the current YOLT_DATABASE_URL from .env and changes only that line, so anything else
in .env stays. Never prints a password.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
ENV, PASSWORD_FILE = ROOT / ".env", ROOT / ".db-password"


def write_private(path: Path, text: str) -> None:
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text)


def with_password(url: str, password: str) -> str:
    """The connection URL with its user's password replaced (or added)."""
    if re.search(r"://[^:/@]+:[^@]*@", url):
        return re.sub(r"(://[^:/@]+):[^@]*@", rf"\g<1>:{password}@", url)
    return re.sub(r"(://[^:/@]+)@", rf"\g<1>:{password}@", url, count=1)


def rotate(env: Path, password_file: Path) -> str:
    """Set a new password on the database and in both files. Only YOLT_DATABASE_URL changes in .env."""
    lines = env.read_text().splitlines()
    url = next((line.split("=", 1)[1] for line in lines if line.startswith("YOLT_DATABASE_URL=")), None)
    if not url:
        raise SystemExit(f"{env} has no YOLT_DATABASE_URL")
    password = secrets.token_hex(24)
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        user = conn.execute("SELECT current_user").fetchone()[0]
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(user), sql.Literal(password)))
    new_url = with_password(url, password)
    with psycopg.connect(new_url, connect_timeout=5):
        pass
    kept = [f"YOLT_DATABASE_URL={new_url}" if line.startswith("YOLT_DATABASE_URL=") else line for line in lines]
    write_private(env, "\n".join(kept) + "\n")
    write_private(password_file, password)
    return user


def main() -> int:
    user = rotate(ENV, PASSWORD_FILE)
    print(f"Rotated the password for {user}; updated YOLT_DATABASE_URL in .env, and .db-password.")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
