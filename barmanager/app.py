import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import __version__, api, web
from .auth import LoginRequired, current_user
from .config import Settings
from .db import Database
from .lookup import Lookup
from .security import hash_password
from .ai import AiWorker, Enricher
from .service import LookupWorker

log = logging.getLogger("barmanager")


def bootstrap_admin(db: Database, settings: Settings) -> None:
    if db.one("SELECT 1 FROM users LIMIT 1"):
        return
    password = settings.admin_password or secrets.token_urlsafe(12)
    db.run("INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?,?,1,?)",
           settings.admin_username, hash_password(password), time.time())
    if settings.admin_password:
        log.warning("Beheerder '%s' aangemaakt met wachtwoord uit BM_ADMIN_PASSWORD.", settings.admin_username)
    else:
        log.warning("Beheerder '%s' aangemaakt met wachtwoord: %s  (wijzig dit via 'Account')",
                    settings.admin_username, password)


def create_app(settings: Settings | None = None, lookup: Lookup | None = None,
               enricher: Enricher | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    Path(settings.image_dir).mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        bootstrap_admin(db, settings)
        from .service import reclassify_all
        n = reclassify_all(db)
        if n:
            log.info("%d product(en) in 'Overig' opnieuw ingedeeld.", n)
        app.state.db = db
        app.state.settings = settings
        app.state.lookup = lookup or Lookup(settings)
        app.state.worker = LookupWorker(db, settings, app.state.lookup)
        app.state.worker.start()
        app.state.ai = AiWorker(db, settings, enricher or Enricher(settings), app.state.lookup)
        app.state.ai.start()
        if app.state.ai.enricher.enabled:
            log.info("AI-aanvulling via %s (%s).", settings.ollama_url, settings.ollama_model)
        if not settings.brave_api_key:
            log.info("Geen BM_BRAVE_API_KEY: opzoeken via Open Food Facts en UPCitemdb.")
        try:
            yield
        finally:
            app.state.worker.stop()
            app.state.ai.stop()
            db.close()

    app = FastAPI(title="BarManager", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    here = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=str(here / "static")), name="static")
    app.mount("/images", StaticFiles(directory=settings.image_dir), name="images")
    app.include_router(api.router)
    app.include_router(web.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        request.state.user = current_user(request)
        return web.render(request, "error.html", status_code=exc.status_code,
                          status=exc.status_code, detail=exc.detail)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": __version__}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
