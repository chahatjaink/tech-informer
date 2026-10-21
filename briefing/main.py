from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from briefing.config import get_settings
from briefing.ingest import fetch_allowlist, ingest_records, load_fixtures, reindex
from briefing.pipeline import run_edition
from briefing.providers import MissingCredential, ProviderError, Router
from briefing.store import PROVIDERS, ROLES, Store


class CredentialIn(BaseModel):
    provider: str
    api_key: str
    model: str = ""


class SettingsIn(BaseModel):
    embed: dict = Field(default_factory=dict)
    roles: dict = Field(default_factory=dict)


CHAT_MODEL = "grok-4.7"
FAST_MODEL = "grok-4.20-0309-non-reasoning"
ROLE_MODELS = {
    "gate": FAST_MODEL,
    "grade": FAST_MODEL,
    "rewrite": FAST_MODEL,
    "rerank": FAST_MODEL,
    "synthesize": CHAT_MODEL,
    "critic": FAST_MODEL,
}
log = logging.getLogger("briefing")


def bootstrap_grok(store: Store) -> None:
    if get_settings().fake_providers:
        return
    key = os.environ.get("GROK_API_KEY", "").strip()
    if not key:
        return
    store.save_credential("xai", key, "env")
    config = store.role_config()
    for role in ROLES:
        config["roles"][role] = {"provider": "xai", "model": ROLE_MODELS[role]}
    config["embed"] = {"provider": "", "model": "", "dimensions": 768}
    store.save_role_config(config)
    log.info("xAI chat roles set to %s from GROK_API_KEY", CHAT_MODEL)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Unauthorized")


def _check_auth(authorization: Optional[str]) -> None:
    settings = get_settings()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()
    token = authorization.split(" ", 1)[1].strip()
    if settings.auth == "dev":
        if not hmac.compare_digest(token, settings.api_token):
            raise _unauthorized()
        return
    try:
        response = httpx.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": token},
            timeout=10,
        )
        response.raise_for_status()
        email = str(response.json().get("email") or "").lower()
    except Exception:
        raise _unauthorized()
    if settings.allowed_email and email != settings.allowed_email:
        raise HTTPException(status_code=403, detail="Email is not on the allowlist")


def _router(store: Store) -> Router:
    settings = get_settings()
    return Router(store, fake_providers=settings.fake_providers)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, MissingCredential):
        return HTTPException(
            status_code=400,
            detail={"error": "missing_credential", "role": exc.role, "provider": exc.provider, "message": str(exc)},
        )
    if isinstance(exc, ProviderError):
        return HTTPException(
            status_code=502,
            detail={"error": "provider", "role": exc.role, "provider": exc.provider, "message": str(exc)},
        )
    return HTTPException(status_code=500, detail=str(exc)[:300])


def create_app() -> FastAPI:
    try:
        bootstrap_grok(Store())
    except Exception as exc:
        log.warning("grok bootstrap skipped: %s", exc)
    app = FastAPI(title="Senior Brief", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        settings = get_settings()
        return {"ok": True, "fake_providers": settings.fake_providers, "auth": settings.auth}

    @app.get("/v1/editions/latest")
    def latest(request: Request, authorization: Optional[str] = Header(default=None)):
        _check_auth(authorization)
        store = Store()
        edition = store.latest_edition()
        if edition is None:
            raise HTTPException(status_code=404, detail="No edition yet")
        if request.headers.get("if-none-match") == edition["etag"]:
            return Response(status_code=304, headers={"ETag": edition["etag"]})
        return JSONResponse(edition["body"], headers={"ETag": edition["etag"]})

    @app.get("/v1/settings")
    def get_settings_route(authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        store = Store()
        return {"config": store.role_config(), "credentials": store.credential_views(), "roles": list(ROLES), "providers": list(PROVIDERS)}

    @app.put("/v1/settings")
    def put_settings(body: SettingsIn, authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        store = Store()
        config = store.save_role_config(body.model_dump())
        return {"config": config}

    @app.post("/v1/credentials")
    def put_credential(body: CredentialIn, authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        if body.provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")
        store = Store()
        router = _router(store)
        model = body.model.strip()
        try:
            if model:
                router.ping(body.provider, body.api_key, model, embed=False)
            saved = store.save_credential(body.provider, body.api_key, "ok" if model else "stored")
        except ProviderError as exc:
            raise _error(exc)
        finally:
            router.close()
        return saved

    @app.post("/v1/credentials/test")
    def test_credential(body: CredentialIn, authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        store = Store()
        router = _router(store)
        api_key = body.api_key.strip() or (store.api_key(body.provider) or "")
        model = body.model.strip()
        if not api_key or not model:
            raise HTTPException(status_code=400, detail="provider, model, and a key are required")
        try:
            router.ping(body.provider, api_key, model, embed=False)
        except ProviderError as exc:
            raise _error(exc)
        finally:
            router.close()
        return {"ok": True, "provider": body.provider}

    @app.delete("/v1/credentials/{provider}")
    def remove_credential(provider: str, authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        Store().delete_credential(provider)
        return {"ok": True}

    @app.post("/v1/ingest")
    def ingest(authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        store = Store()
        router = _router(store)
        try:
            records, errors = fetch_allowlist()
            report = ingest_records(store, router, records)
            report["errors"] = errors + report["errors"]
            report["reindex"] = reindex(store, router)
            return report
        finally:
            router.close()

    @app.post("/v1/editions/run")
    def run(authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        store = Store()
        router = _router(store)
        try:
            return run_edition(store, router)
        except (ProviderError, MissingCredential) as exc:
            raise _error(exc)
        finally:
            router.close()

    @app.post("/v1/jobs/daily")
    def daily(authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        store = Store()
        router = _router(store)
        try:
            records, errors = fetch_allowlist()
            report = ingest_records(store, router, records)
            report["errors"] = errors + report["errors"]
            report["reindex"] = reindex(store, router)
            edition = run_edition(store, router)
            return {"ingest": report, "edition_id": edition["edition_id"]}
        except (ProviderError, MissingCredential) as exc:
            raise _error(exc)
        finally:
            router.close()

    @app.post("/v1/jobs/demo")
    def demo(authorization: Optional[str] = Header(default=None)) -> dict:
        _check_auth(authorization)
        settings = get_settings()
        if not settings.fake_providers:
            raise HTTPException(status_code=400, detail="Demo editions require BRIEF_FAKE_PROVIDERS=1")
        store = Store()
        router = _router(store)
        try:
            report = ingest_records(store, router, load_fixtures())
            edition = run_edition(store, router)
            return {"ingest": report, "edition": edition}
        except (ProviderError, MissingCredential) as exc:
            raise _error(exc)
        finally:
            router.close()

    extension = get_settings().extension_dir
    if extension.exists():
        app.mount("/panel", StaticFiles(directory=extension, html=True), name="panel")

        @app.get("/")
        def root() -> RedirectResponse:
            return RedirectResponse("/panel/sidepanel.html")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("briefing.main:app", host="127.0.0.1", port=8787, reload=False)
