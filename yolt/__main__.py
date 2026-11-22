"""Run Yolt locally: ``python -m yolt`` (or ``make``)."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .db import DatabaseUnavailable
from .server import LOOPBACK_HOSTS, create_app

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed_hosts(host: str) -> list[str]:
    """The Host headers to accept: the usual loopback names plus the one Yolt listens on (127.0.0.2, ...)."""
    return list(dict.fromkeys([*LOOPBACK_HOSTS, host]))


def main() -> int:
    parser = argparse.ArgumentParser(prog="yolt", description="Paste a YouTube link, get clips.")
    parser.add_argument("--host", default=os.environ.get("YOLT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("YOLT_PORT", "8484")))
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("YOLT_DATA_DIR", DEFAULT_DATA_DIR)))
    parser.add_argument("--dev", action="store_true",
                        help="Restart on Python changes and live-reload the browser on any UI change.")
    args = parser.parse_args()

    # Local mode has no accounts or auth, so it only ever listens on loopback.
    # Reach it from another machine with an SSH tunnel:
    #   ssh -L 8484:127.0.0.1:8484 <host>
    if not is_loopback(args.host):
        print(
            f"Refusing to listen on {args.host}: local mode has no authentication and "
            "only binds to loopback. Use an SSH tunnel to reach it from another machine.",
            file=sys.stderr,
        )
        return 2
    if ":" in args.host:
        # The Host check can't match a bracketed IPv6 address, so every request would be refused.
        print(f"Refusing to listen on {args.host}: use 127.0.0.1 (or localhost) instead of IPv6 loopback.",
              file=sys.stderr)
        return 2

    if not os.environ.get("YOLT_DATABASE_URL"):
        print("Set YOLT_DATABASE_URL to Yolt's PostgreSQL database. `make db` starts one and writes it to .env.",
              file=sys.stderr)
        return 2

    data_dir = args.data_dir.expanduser().resolve()
    print(f"Yolt running at http://{args.host}:{args.port}{' (dev: live reload)' if args.dev else ''}",
          flush=True)
    if args.dev:
        # The reloader re-imports the app in a fresh process, so pass settings by env.
        os.environ["YOLT_DATA_DIR"] = str(data_dir)
        os.environ["YOLT_HOST"] = args.host
        uvicorn.run("yolt.__main__:dev_app", factory=True, reload=True,
                    reload_dirs=[str(Path(__file__).parent)], host=args.host, port=args.port,
                    log_level="warning", timeout_graceful_shutdown=2)
        return 0
    _logging()
    try:
        app = create_app(data_dir, allowed_hosts=allowed_hosts(args.host))
    except DatabaseUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def dev_app():
    """App factory used by ``--dev``; uvicorn calls it again after each reload."""
    _logging()
    return create_app(Path(os.environ["YOLT_DATA_DIR"]), dev=True,
                      allowed_hosts=allowed_hosts(os.environ.get("YOLT_HOST", "127.0.0.1")))


if __name__ == "__main__":
    raise SystemExit(main())
