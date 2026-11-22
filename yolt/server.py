"""The app: the API (yolt/api.py), its docs, and the web UI."""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import api, docs, services
from .api import PREFIX, respond
from .db import Database
from .embeddings import EmbeddingService
from .pipeline import Worker
from .problems import ApiProblem, unstorable
from .repo import clips as clips_repo
from .subscriptions import PAUSE_SECONDS

STATIC_DIR = Path(__file__).parent / "static"
SPEC_PATH = Path(__file__).resolve().parent.parent / "spec" / "yolt.openapi.json"
UI_FILES = ("index.html", "app.js", "app.css", "tv.js", "tv.css")
LABS_DIR = STATIC_DIR / "labs"  # each lab's UI module (yolt/labs/__init__.py)
LOOPBACK_HOSTS = ["127.0.0.1", "localhost"]

# Dev mode only: reload the page when the server restarts or a UI file changes.
LIVE_RELOAD = """<script>
(() => {
  let seen;
  const tick = async () => {
    try {
      const version = await (await fetch('/api/dev/version', { cache: 'no-store' })).text();
      if (seen && version !== seen) location.reload();
      seen = version;
    } catch {}
    setTimeout(tick, 1000);
  };
  tick();
})();
</script>
"""


class SecurityHeaders:
    """Adds api.SECURITY_HEADERS to every response that doesn't set its own: other sites can't frame Yolt
    (to clickjack Subscribe or Retry), and browsers don't sniff content types. Only framing is restricted:
    the UI embeds YouTube players and the dev live reload is an inline script."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in api.SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text())


def create_app(
    data_dir: Path,
    *,
    database_url: str | None = None,
    setting_defaults: dict[str, str] | None = None,
    start_worker: bool = True,
    allowed_hosts: list[str] | None = None,
    client_factory: Callable[[], httpx.Client] = httpx.Client,
    poll_seconds: float = services.POLL_SECONDS,
    subscription_pause_seconds: float = PAUSE_SECONDS,
    dev: bool = False,
) -> FastAPI:
    database_url = database_url or os.environ.get("YOLT_DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set YOLT_DATABASE_URL to Yolt's PostgreSQL database (`make db` creates one).")
    db = Database(database_url, setting_defaults=setting_defaults)
    embeddings = EmbeddingService(db.settings, client_factory)
    worker = Worker(db, data_dir, embeddings=embeddings, client_factory=client_factory, poll_seconds=poll_seconds,
                    subscription_pause_seconds=subscription_pause_seconds)
    worker.thumbs_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.open()
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()
            db.close()

    # The hand-written spec is the contract, so FastAPI's generated one is off.
    app = FastAPI(title="Yolt API", version=api.API_VERSION, lifespan=lifespan,
                  openapi_url=None, docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.worker = worker
    app.state.embeddings = embeddings
    app.state.client_factory = client_factory

    # Local mode serves only this machine. Rejecting foreign Host headers blocks
    # DNS-rebinding, and rejecting foreign Origins blocks other sites from
    # driving the API through the user's browser.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOOPBACK_HOSTS)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return respond(403, ApiProblem(403, "cross-origin", "Cross-origin request blocked",
                                               detail="Writes are only accepted from Yolt's own pages.").body())
        return await call_next(request)

    # Added last, so it's outermost and covers every response above, the host check's included.
    app.add_middleware(SecurityHeaders)

    api.install(app)

    # --- docs ---

    spec = load_spec()
    spec_bytes = json.dumps(spec, ensure_ascii=False).encode()
    rendered: dict[str, str] = {}

    @app.get("/api/openapi.json", include_in_schema=False)
    def openapi_spec():
        return Response(spec_bytes, media_type="application/json")

    @app.get("/api/docs", include_in_schema=False)
    def api_docs(request: Request):
        base = str(request.base_url).rstrip("/") + PREFIX
        if base not in rendered:  # one render per host the docs are viewed through
            rendered[base] = docs.render_page(spec, title=f"{spec['info']['title']} — docs", base_url=base)
        return HTMLResponse(rendered[base])

    # --- web UI ---

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/media/thumbs", StaticFiles(directory=worker.thumbs_dir), name="thumbs")
    boot_id = secrets.token_hex(4)

    def asset_version() -> str:
        """Changes whenever a UI file does, so browsers never run stale code."""
        labs = sorted([*LABS_DIR.glob("*.js"), *LABS_DIR.glob("*.css")])
        stamps = [(STATIC_DIR / n).stat() for n in UI_FILES] + [p.stat() for p in labs]
        return hashlib.sha1(repr([(st.st_mtime_ns, st.st_size) for st in stamps]).encode()).hexdigest()[:10]

    if dev:
        @app.get("/api/dev/version", include_in_schema=False)
        def dev_version():
            return PlainTextResponse(f"{boot_id}-{asset_version()}", headers={"Cache-Control": "no-store"})

    def page(meta: dict[str, str] | None = None) -> HTMLResponse:
        meta = meta or {}
        tags = "\n".join(
            f'<meta property="{html.escape(k)}" content="{html.escape(v)}">' for k, v in meta.items()
        )
        title = html.escape(meta.get("og:title", "Yolt"))
        body = ((STATIC_DIR / "index.html").read_text()
                .replace("<!--META-->", tags)
                .replace("<title>Yolt</title>", f"<title>{title}</title>")
                .replace("__VERSION__", asset_version()))
        if dev:
            body = body.replace("</body>", f"{LIVE_RELOAD}</body>")
        return HTMLResponse(body, headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    @app.get("/r/{run_id}", include_in_schema=False)
    @app.get("/search", include_in_schema=False)
    @app.get("/themes", include_in_schema=False)
    @app.get("/subscriptions", include_in_schema=False)
    @app.get("/episodes", include_in_schema=False)
    @app.get("/saved", include_in_schema=False)
    @app.get("/tv", include_in_schema=False)
    @app.get("/compilations", include_in_schema=False)
    @app.get("/labs/{lab}", include_in_schema=False)
    def ui():
        return page()

    @app.get("/c/{slug}", include_in_schema=False)
    def shared_clip(request: Request, slug: str):
        """The share link: the UI, with the clip's title and frame for link previews."""
        if unstorable(slug):  # no such clip, and PostgreSQL can't be asked
            return page()
        try:
            with db.connect() as conn:
                clip = clips_repo.get_shared(conn, slug, set(), str(request.base_url))
        except ApiProblem:
            return page()
        return page({
            "og:title": clip["title"],
            "og:description": (clip["text"] or clip["summary"] or "")[:200],
            "og:image": clip["links"]["video_thumbnail"],
            "og:type": "video.other",
        })

    return app
