"""A throwaway PostgreSQL cluster for tests.

Listens on a Unix socket in a private temp dir only (listen_addresses=''), so
nothing is reachable over the network; trust auth is confined to that socket.
Each test gets its own database cloned from a migrated template.
"""
from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg


def _bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in sorted(Path("/opt/homebrew/opt").glob("postgresql@*/bin/" + name), reverse=True):
        return str(candidate)
    raise RuntimeError(f"{name} not found; install PostgreSQL 16 (brew install postgresql@16)")


class TempPostgres:
    def __init__(self) -> None:
        # Short path: Unix socket paths are limited to ~100 bytes.
        self.root = Path(tempfile.mkdtemp(prefix="yoltpg", dir="/tmp"))
        self.data = self.root / "data"
        self.port = 5400 + os.getpid() % 500
        self._counter = itertools.count()

    def start(self) -> "TempPostgres":
        subprocess.run([_bin("initdb"), "-D", str(self.data), "-U", "postgres", "-A", "trust", "--no-sync",
                        "-E", "UTF8", "--locale=C"], check=True, capture_output=True)
        subprocess.run([_bin("pg_ctl"), "-D", str(self.data), "-l", str(self.root / "log"), "-w", "-o",
                        f"-c listen_addresses='' -k {self.root} -p {self.port} -c fsync=off "
                        f"-c synchronous_commit=off -c full_page_writes=off -c max_connections=300",
                        "start"], check=True, capture_output=True)
        deadline = time.time() + 15
        while True:
            try:
                with psycopg.connect(self.dsn("postgres")):
                    return self
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)

    def stop(self) -> None:
        subprocess.run([_bin("pg_ctl"), "-D", str(self.data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def dsn(self, dbname: str) -> str:
        return f"postgresql://postgres@/{dbname}?host={self.root}&port={self.port}"

    def create(self, name: str, template: str | None = None) -> str:
        with psycopg.connect(self.dsn("postgres"), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))
        return self.dsn(name)

    def clone(self, template: str) -> str:
        return self.create(f"t{next(self._counter)}_{os.getpid()}", template)
