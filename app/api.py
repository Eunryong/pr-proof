"""진입점: FastAPI 앱. `uvicorn app.api:app --port 8000`."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis import Redis

from app import config
from app.controllers import webhook


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = config.load_config()
    app.state.secret = config.read_secret(config.WEBHOOK_SECRET)
    app.state.allowed_repos = frozenset(settings.repos)
    app.state.redis = Redis.from_url(settings.redis_url)
    yield
    app.state.redis.close()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {}
    result = webhook.handle(
        body=body,
        payload=payload,
        event=request.headers.get("X-GitHub-Event"),
        delivery_id=request.headers.get("X-GitHub-Delivery"),
        signature_header=request.headers.get("X-Hub-Signature-256"),
        secret=request.app.state.secret,
        allowed_repos=request.app.state.allowed_repos,
        redis=request.app.state.redis,
    )
    return JSONResponse(result.body, status_code=result.status_code)
