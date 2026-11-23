"""진입점: FastAPI 앱. `uvicorn app.api.main:app --port 8765`.

웹훅을 받고, 같은 프로세스에서 동기화 루프를 주기적으로 돌린다 (docs/DESIGN.md F2).
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis import Redis
from redis.exceptions import RedisError

from app.api import sync, webhook
from app.api.history import History
from app.shared import config
from app.shared.ops_alert_pure import Kind

log = logging.getLogger(__name__)


async def sync_loop(
    settings: config.PipelineConfig,
    redis: Redis,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """시작할 때 한 번, 이후 interval_minutes마다 동기화한다. gh 호출은 스레드에서 돌려
    웹훅 응답을 막지 않는다. 한 번 실패해도 루프는 계속 돈다. 두 주기 연속으로 gh 로그인이나
    Redis가 실패하면 macOS 알림을 띄운다 (6-4)."""
    history = History(settings.db_path)
    failing: frozenset[Kind] = frozenset()
    while True:
        current: frozenset[Kind] = frozenset()
        try:
            result = await asyncio.to_thread(sync.run_once, settings, redis, history)
            current = result.environment
            if result.enqueued:
                log.info("동기화로 등록: %s", result.enqueued)
        except RedisError:
            log.exception("동기화 실패: Redis")
            current = frozenset({"redis"})
        except Exception:  # 루프가 죽으면 동기화가 조용히 멈춘다
            log.exception("동기화 실패")
        sync.alert_lasting(failing, current, settings.data_dir)
        failing = current
        await sleep(settings.sync.interval_minutes * 60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = config.load_config()
    app.state.secret = config.read_secret(config.WEBHOOK_SECRET)
    app.state.allowed_repos = frozenset(settings.repos)
    app.state.redis = Redis.from_url(settings.redis_url)
    task = None
    if settings.sync.interval_minutes > 0:
        task = asyncio.create_task(sync_loop(settings, app.state.redis))
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
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
