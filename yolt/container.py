"""Run Yolt inside Compose, where the host publishes its port on loopback."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from .__main__ import _logging
from .server import create_app

PORT = 8485
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
SERVICES = {
    "parakeet_url": ("YOLT_PARAKEET_URL", 5092, "parakeet-cpu"),
    "highlightr_url": ("YOLT_HIGHLIGHTR_URL", 8000, "highlightr-api"),
}


def app():
    database_url = os.environ.get("YOLT_DATABASE_URL")
    if not database_url:
        raise SystemExit("YOLT_DATABASE_URL is missing. Run `make .env` before starting Yolt with Compose.")
    database = urlsplit(database_url)
    if (database.hostname in LOOPBACK
            and database.port == int(os.environ.get("YOLT_DB_PORT", "5489"))):
        credentials, separator, _ = database.netloc.rpartition("@")
        database_url = database._replace(netloc=f"{credentials}{separator}db:5432").geturl()

    defaults = {}
    for key, (variable, port, service) in SERVICES.items():
        address = f"http://{service}:{port}"
        defaults[key] = address
        # Existing .env files use the ports exposed to the host. Translate those
        # addresses to the matching Compose services when used inside the container.
        value = os.environ.get(variable, "").rstrip("/")
        if value in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            os.environ[variable] = address

    return create_app(Path("/data"), database_url=database_url, setting_defaults=defaults)


if __name__ == "__main__":
    _logging()
    uvicorn.run(app, factory=True, host="0.0.0.0", port=PORT, log_level="warning")
