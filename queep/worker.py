"""Private Cloud Run entrypoint. Tasks contain identifiers, never reference content."""

import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict, Field

from queep.ai import AI, AIError, document
from queep.budget import Budget
from queep.config import Settings, database, firebase_app, image_bucket
from queep.dispatch import enqueue
from queep.images import Images
from queep.jobs import Jobs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(ge=1)
    attempt: int = Field(ge=0, le=3)


def authorized(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    settings = Settings()
    # Local worker is driven by CLI, not an unauthenticated network endpoint.
    if not credentials or not settings.worker_url or not settings.task_identity:
        raise HTTPException(401, "Worker privado")
    try:
        claims = id_token.verify_oauth2_token(credentials.credentials, Request(),
                                             settings.worker_url)
        if claims.get("email") != settings.task_identity or not claims.get("email_verified"):
            raise ValueError("identity")
    except ValueError as exc:
        raise HTTPException(403, "Identidade não autorizada") from exc


@lru_cache
def engine():
    settings = Settings()
    if not settings.ai_enabled:
        raise HTTPException(503, "Processamento de IA indisponível")
    return AI(settings.openrouter_key.get_secret_value(),
              Budget(database(), settings.ai_daily_budget))


def process(task, jobs, ai, images):
    work = jobs.claim(task)
    if not work:
        return "ignored"
    try:
        ref = work["reference"]
        data = images.read(work["uid"], ref.id)[0] if ref.kind == "image" else None
        if not jobs.current(work):
            return "stale"
        analysis = ai.interpret(ref, data)
        if not jobs.current(work):
            return "stale"
        vector = ai.embed(document(ref, analysis))
        return "ready" if jobs.finish(work, analysis=analysis, vector=vector) else "stale"
    except (AIError, HTTPException) as error:
        code = str(error) if isinstance(error, AIError) else f"http_{error.status_code}"
        jobs.finish(work, error=code)
        return "retry_scheduled"
    except Exception:
        logger.warning("ai_processing_failed")
        jobs.finish(work, error="processing_failed")
        return "retry_scheduled"


@asynccontextmanager
async def lifespan(app):
    Settings()
    firebase_app()
    yield


app = FastAPI(title="Queep private worker", lifespan=lifespan,
              dependencies=[Depends(authorized)])


@app.post("/tasks/process")
def handle(task: Task):
    return {"status": process(task.model_dump(), Jobs(database()), engine(),
                              Images(database(), image_bucket()))}


@app.post("/tasks/dispatch")
def dispatch():
    jobs, settings, count = Jobs(database()), Settings(), 0
    for doc in jobs.due():
        try:
            count += int(enqueue(jobs.payload(doc), settings, jobs))
        except Exception:
            logger.warning("ai_dispatch_failed")
    return {"enqueued": count}


if __name__ == "__main__":
    if Settings().environment != "local":
        raise SystemExit("CLI restricted to emulators")
    jobs = Jobs(database())
    for doc in jobs.due():
        print(process(jobs.payload(doc), jobs, engine(), Images(database(), image_bucket())))
