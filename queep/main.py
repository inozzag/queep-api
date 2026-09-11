from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import anyio
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth
from google.api_core.exceptions import DeadlineExceeded, FailedPrecondition, ServiceUnavailable
from pydantic import BaseModel, Field

from queep.account_state import deletion_document, require_open
from queep.accounts import Accounts
from queep.ai import AI, AIError
from queep.billing import Access, Billing, Webhook
from queep.config import Settings, database, firebase_app, image_bucket
from queep.dispatch import dispatch_one
from queep.images import MAX_BYTES, Images
from queep.jobs import Jobs
from queep.models import Capture, Edit, Library, Reference, RetryAnalysis, Search, SearchResults
from queep.repository import Repository
from queep.search import search
from queep.worker import engine


@asynccontextmanager
async def lifespan(app):
    Settings()  # Fail closed on missing or unsafe environment configuration.
    firebase_app()
    yield


app = FastAPI(title="Queep API", version="0.1.0", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


def user(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not credentials:
        raise HTTPException(401, "Entre na sua conta", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = auth.verify_id_token(
            credentials.credentials, app=firebase_app(), check_revoked=True,
        )
    except (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError,
            auth.UserNotFoundError, ValueError) as exc:
        raise HTTPException(401, "Sessão inválida", headers={"WWW-Authenticate": "Bearer"}) from exc
    require_open(database(), claims["uid"])
    return claims["uid"]


UID = Annotated[str, Depends(user)]


def billing():
    return Billing(database(), Settings())


Subscriptions = Annotated[Billing, Depends(billing)]


def paid_user(uid: UID, subscriptions: Subscriptions):
    subscriptions.require(uid)
    return uid


PaidUID = Annotated[str, Depends(paid_user)]


def repository():
    return Repository(database())


Repo = Annotated[Repository, Depends(repository)]


def images():
    return Images(database(), image_bucket())


Media = Annotated[Images, Depends(images)]
# Image decoding is memory intensive. Limit transformations per API process.
image_limiter = anyio.CapacityLimiter(1)


@app.middleware("http")
async def privacy_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health")
@app.get("/healthz", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.get("/v1/billing/access", response_model=Access)
def subscription_access(uid: UID, subscriptions: Subscriptions):
    return subscriptions.access(uid)


class AccountChange(BaseModel):
    operation_id: UUID
    version: int = Field(ge=0)
    immediate: bool = False


def account_claims(credentials, recent=False, revoked=True):
    if not credentials:
        raise HTTPException(401, "Entre na sua conta")
    try:
        claims = auth.verify_id_token(credentials.credentials, app=firebase_app(),
                                      check_revoked=revoked)
        if recent:
            age = datetime.now(UTC).timestamp() - claims.get("auth_time", 0)
            if not 0 <= age <= 300:
                raise HTTPException(403, "Confirme sua senha novamente para alterar a exclusão.")
        return claims
    except (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError,
            auth.UserNotFoundError, ValueError) as exc:
        raise HTTPException(401, "Sessão inválida") from exc


@app.post("/v1/account/deletion", status_code=202)
def delete_account(change: AccountChange,
                   credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
                   background: BackgroundTasks):
    claims = account_claims(credentials, revoked=False)
    uid = claims["uid"]
    existing = deletion_document(database(), uid).get().to_dict() or {}
    service = Accounts(database(), image_bucket(), Settings())
    # Only an exact retry is accepted after the user has been permanently erased.
    if existing.get("operation_id") == str(change.operation_id):
        return service.status(existing)
    if not Settings().account_deletion_enabled:
        raise HTTPException(503, "A exclusão de conta ainda não está disponível.")
    account_claims(credentials, recent=True)
    result = service.request(uid, immediate=change.immediate, version=change.version,
                             operation_id=str(change.operation_id))
    if result["status"] == "deleting":
        background.add_task(service.resume, uid)
    return result


@app.post("/v1/account/deletion/cancel")
def cancel_account_deletion(
    change: AccountChange,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    uid = account_claims(credentials, recent=True)["uid"]
    return Accounts(database(), image_bucket(), Settings()).cancel(
        uid, change.version, str(change.operation_id))


@app.get("/v1/account")
def account_status(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    uid = account_claims(credentials)["uid"]
    return Accounts(database(), image_bucket(), Settings()).get_status(uid)


@app.post("/v1/billing/sync", response_model=Access)
def sync_subscription(uid: UID, subscriptions: Subscriptions):
    return subscriptions.access(uid, force=True)


@app.post("/v1/billing/webhook")
async def subscription_event(request: Request, subscriptions: Subscriptions):
    subscriptions.authenticate_webhook(request.headers.get("authorization"))
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 65536:
            raise HTTPException(413, "Evento muito grande")
        raw.extend(chunk)
    from pydantic import ValidationError
    try:
        event = Webhook.model_validate_json(bytes(raw)).event
    except ValidationError as error:
        raise HTTPException(422, "Evento inválido") from error
    await anyio.to_thread.run_sync(subscriptions.event, event)
    return {"received": True}


@app.put("/v1/references/{ref_id}", response_model=Reference)
def create(ref_id: UUID, capture: Capture, uid: PaidUID, repo: Repo, background: BackgroundTasks):
    reference = repo.save(uid, str(ref_id), capture)
    background.add_task(dispatch_one, uid, str(ref_id))
    return reference


@app.put("/v1/references/{ref_id}/image", response_model=Reference, openapi_extra={
    "requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    }},
})
async def upload_image(ref_id: UUID, request: Request, uid: PaidUID, media: Media,
                       background: BackgroundTasks,
                       title: Annotated[str, Query(max_length=200)] = ""):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "Escolha uma imagem de até 12 MB")
        data.extend(chunk)
    reference = await anyio.to_thread.run_sync(media.upload, uid, str(ref_id), title.strip(),
                                             bytes(data), limiter=image_limiter)
    background.add_task(dispatch_one, uid, str(ref_id))
    return reference


@app.get("/v1/references/{ref_id}/image", responses={200: {"content": {
    "image/jpeg": {}, "image/png": {},
}}})
def read_image(ref_id: UUID, uid: UID, media: Media, thumbnail: bool = False):
    data, content_type = media.read(uid, str(ref_id), thumbnail)
    return Response(content=data, media_type=content_type,
                    headers={"Content-Disposition": "inline"})


@app.get("/v1/references", response_model=Library)
def library(uid: UID, repo: Repo, limit: Annotated[int, Query(ge=1, le=100)] = 40,
            cursor: Annotated[str | None, Query(max_length=1000)] = None):
    return repo.list(uid, limit, cursor)


@app.get("/v1/references/{ref_id}", response_model=Reference)
def get(ref_id: UUID, uid: UID, repo: Repo):
    return repo.get(uid, str(ref_id))


@app.patch("/v1/references/{ref_id}", response_model=Reference)
def edit(ref_id: UUID, capture: Edit, uid: PaidUID, repo: Repo, background: BackgroundTasks):
    reference = repo.save(uid, str(ref_id), capture, editing=True)
    background.add_task(dispatch_one, uid, str(ref_id))
    return reference


@app.delete("/v1/references/{ref_id}", status_code=204)
def delete(ref_id: UUID, uid: UID, repo: Repo, media: Media,
           revision: Annotated[int, Query(ge=1)]):
    repo.delete(uid, str(ref_id), revision)
    media.cleanup(uid, str(ref_id))
    return Response(status_code=204)


@app.post("/v1/references/{ref_id}/retry", response_model=Reference)
def retry_analysis(ref_id: UUID, capture: RetryAnalysis, uid: PaidUID,
                   background: BackgroundTasks):
    reference = Jobs(database()).retry(uid, str(ref_id), capture.revision)
    background.add_task(dispatch_one, uid, str(ref_id))
    return reference


@app.post("/v1/search", response_model=SearchResults)
def find_references(capture: Search, uid: PaidUID, repo: Repo, ai: Annotated[AI, Depends(engine)]):
    try:
        return search(repo, ai, uid, capture.query)
    except (AIError, FailedPrecondition, DeadlineExceeded, ServiceUnavailable) as exc:
        raise HTTPException(
            503, "A busca está temporariamente indisponível. Tente novamente.",
        ) from exc
