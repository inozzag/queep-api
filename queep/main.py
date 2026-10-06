from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
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
from queep.availability import auth_retry
from queep.billing import Access, Billing, Webhook
from queep.config import Settings, database, firebase_app, image_bucket
from queep.dispatch import dispatch_one
from queep.images import MAX_BYTES, Images
from queep.jobs import Jobs
from queep.media import MAX_BYTES as MAX_MEDIA_BYTES
from queep.media import MediaFiles
from queep.models import (
    AccountStatus,
    APIError,
    ArchiveEdit,
    BatchArchive,
    Capture,
    Edit,
    ImageInfo,
    Library,
    MetadataEdit,
    OnboardingProfile,
    OnboardingRecord,
    Reference,
    RetryAnalysis,
    Search,
    SearchResults,
    Space,
    SpaceEdit,
    SpaceMembership,
    SpaceName,
    UsageSnapshot,
)
from queep.repository import Repository
from queep.search import search
from queep.spaces import Spaces
from queep.usage import snapshot
from queep.worker import engine


@asynccontextmanager
async def lifespan(app):
    Settings()  # Fail closed on missing or unsafe environment configuration.
    firebase_app()
    yield


app = FastAPI(title="Queep API", version="0.1.0", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


_REJECTED_AUTH = (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError,
                  auth.UserNotFoundError, ValueError)


def verified_user(credentials, *, recent=False, revoked=True, headers=None):
    if not credentials:
        raise HTTPException(401, "Entre na sua conta", headers=headers)
    try:
        claims = auth.verify_id_token(credentials.credentials, app=firebase_app(),
                                      check_revoked=revoked)
        if recent:
            age = datetime.now(UTC).timestamp() - claims.get("auth_time", 0)
            if not 0 <= age <= 300:
                raise HTTPException(403,
                                    "Confirme sua identidade novamente para alterar a exclusão.")
        return claims
    except HTTPException:
        raise
    except _REJECTED_AUTH as exc:
        raise HTTPException(401, "Sessão inválida", headers=headers) from exc
    except Exception as exc:
        mapped = auth_retry(exc)
        if mapped is not None:
            raise mapped from exc
        raise


def user(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    claims = verified_user(credentials, headers={"WWW-Authenticate": "Bearer"})
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


_UNAUTHORIZED = {"description": "Sessão inválida", "model": APIError}
_RETRY = {
    "description": "Falha transitória. Tente novamente após Retry-After.",
    "model": APIError,
    "headers": {"Retry-After": {"description": "Segundos até repetir a leitura.",
                                "schema": {"type": "string"}}},
}


@app.get("/v1/usage", response_model=UsageSnapshot, responses={401: _UNAUTHORIZED, 503: _RETRY})
def usage(uid: UID, subscriptions: Subscriptions):
    return snapshot(database(), uid, Settings(), access=subscriptions.access(uid))


class AccountChange(BaseModel):
    operation_id: UUID
    version: int = Field(ge=0)
    immediate: bool = False


def account_claims(credentials, recent=False, revoked=True):
    return verified_user(credentials, recent=recent, revoked=revoked)


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


@app.get("/v1/account/onboarding")
def read_onboarding(uid: UID):
    record = Accounts(database(), image_bucket(), Settings()).get_onboarding(uid)
    if not record:
        return Response(status_code=204)
    return record


@app.put("/v1/account/onboarding", response_model=OnboardingRecord)
def write_onboarding(profile: OnboardingProfile, uid: UID):
    return Accounts(database(), image_bucket(), Settings()).save_onboarding(uid, profile)


@app.get("/v1/account", response_model=AccountStatus,
         responses={401: _UNAUTHORIZED, 503: _RETRY})
def account_status(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    uid = account_claims(credentials)["uid"]
    return Accounts(database(), image_bucket(), Settings()).get_status(uid)


@app.put("/v1/account/avatar", response_model=ImageInfo)
async def upload_avatar(request: Request, uid: UID):
    async with image_limiter:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Escolha uma foto de até 12 MB")
            data.extend(chunk)
        service = Accounts(database(), image_bucket(), Settings())
        return await anyio.to_thread.run_sync(service.put_avatar, uid, bytes(data))


@app.get("/v1/account/avatar")
def read_avatar(uid: UID, thumbnail: bool = True):
    result = Accounts(database(), image_bucket(), Settings()).get_avatar(uid, thumbnail)
    if not result:
        return Response(status_code=204)
    data, content_type = result
    return Response(content=data, media_type=content_type,
                    headers={"Content-Disposition": "inline"})


@app.delete("/v1/account/avatar", status_code=204)
def remove_avatar(uid: UID):
    Accounts(database(), image_bucket(), Settings()).delete_avatar(uid)


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


@app.post("/v1/references", response_model=Reference, status_code=201)
def capture_reference(capture: Capture, uid: PaidUID, repo: Repo, background: BackgroundTasks):
    from uuid import uuid4
    return create(uuid4(), capture, uid, repo, background)


@app.get("/v1/spaces", response_model=list[Space])
def list_spaces(uid: UID):
    return Spaces(database()).list(uid)


@app.post("/v1/spaces", response_model=Space, status_code=201)
def create_space(change: SpaceName, uid: UID):
    return Spaces(database()).save(uid, change)


@app.patch("/v1/spaces/{space_id}", response_model=Space)
def edit_space(space_id: UUID, change: SpaceEdit, uid: UID):
    return Spaces(database()).save(uid, change, str(space_id))


@app.delete("/v1/spaces/{space_id}", status_code=204)
def delete_space(space_id: UUID, uid: UID, background: BackgroundTasks):
    service = Spaces(database())
    service.delete(uid, str(space_id))
    # Durable cleanup is also resumed by the private dispatch worker after a crash.
    background.add_task(service.cleanup, uid, str(space_id))
    return Response(status_code=204)


@app.post("/v1/references/{ref_id}/spaces", response_model=Reference)
def assign_spaces(ref_id: UUID, change: SpaceMembership, uid: UID):
    return Spaces(database()).assign(uid, str(ref_id), change)


@app.put("/v1/references/{ref_id}/image", response_model=Reference, openapi_extra={
    "requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    }},
})
async def upload_image(ref_id: UUID, request: Request, uid: PaidUID, media: Media,
                       background: BackgroundTasks,
                       title: Annotated[str, Query(max_length=200)] = "",
                       space_ids: Annotated[list[UUID] | None, Query(max_length=40)] = None):
    async with image_limiter:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Escolha uma imagem de até 12 MB")
            data.extend(chunk)
        reference = await anyio.to_thread.run_sync(media.upload, uid, str(ref_id), title.strip(),
                                                 bytes(data), list(map(str, space_ids or [])))
    background.add_task(dispatch_one, uid, str(ref_id))
    return reference


@app.put("/v1/references/{ref_id}/media", response_model=Reference)
async def upload_media(ref_id: UUID, request: Request, uid: UID, subscriptions: Subscriptions,
                       background: BackgroundTasks,
                       title: Annotated[str, Query(max_length=200)] = "",
                       space_ids: Annotated[list[UUID] | None, Query(max_length=40)] = None):
    subscriptions.require(uid, "media_av")
    # Bound concurrent buffering as well as decoder work.
    async with image_limiter:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_MEDIA_BYTES:
                raise HTTPException(413, "Escolha um áudio ou vídeo de até 25 MB.")
            data.extend(chunk)
        service = MediaFiles(database(), image_bucket())
        reference = await anyio.to_thread.run_sync(service.upload, uid, str(ref_id),
                                                  title.strip(), bytes(data),
                                                  list(map(str, space_ids or [])))
    background.add_task(dispatch_one, uid, str(ref_id))
    return reference


@app.get("/v1/references/{ref_id}/media")
def read_media(ref_id: UUID, uid: UID, thumbnail: bool = False, loop: bool = False):
    data, content_type = MediaFiles(database(), image_bucket()).read(
        uid, str(ref_id), thumbnail, loop)
    return Response(content=data, media_type=content_type,
                    headers={"Content-Disposition": "inline"})


@app.get("/v1/references/{ref_id}/image", responses={200: {"content": {
    "image/jpeg": {}, "image/png": {}, "video/mp4": {},
}}})
def read_image(ref_id: UUID, uid: UID, media: Media, thumbnail: bool = False,
               index: Annotated[int, Query(ge=0, le=3)] = 0,
               role: Annotated[Literal["gallery", "author"], Query()] = "gallery",
               motion: bool = False):
    data, content_type = media.read(uid, str(ref_id), thumbnail, index, role, motion)
    return Response(content=data, media_type=content_type,
                    headers={"Content-Disposition": "inline"})


@app.get("/v1/references", response_model=Library)
def library(uid: UID, repo: Repo, limit: Annotated[int, Query(ge=1, le=100)] = 40,
            cursor: Annotated[str | None, Query(max_length=1000)] = None,
            archived: Annotated[Literal["exclude", "only"], Query()] = "exclude",
            space_id: UUID | None = None):
    return repo.list(uid, limit, cursor, archived=archived == "only",
                     space_id=str(space_id) if space_id else None)


@app.get("/v1/references/{ref_id}", response_model=Reference)
def get(ref_id: UUID, uid: UID, repo: Repo):
    return repo.get(uid, str(ref_id))


@app.patch("/v1/references/{ref_id}", response_model=Reference)
def edit(ref_id: UUID, capture: Edit | MetadataEdit | ArchiveEdit, uid: UID, repo: Repo,
         subscriptions: Subscriptions, background: BackgroundTasks):
    if isinstance(capture, ArchiveEdit):
        return repo.archive(uid, str(ref_id), capture.archived)
    subscriptions.require(uid)
    if isinstance(capture, MetadataEdit):
        reference = repo.edit_metadata(uid, str(ref_id), capture)
    else:
        reference = repo.save(uid, str(ref_id), capture, editing=True)
    background.add_task(dispatch_one, uid, str(ref_id))
    return reference


@app.post("/v1/references/batch-archive", response_model=Library)
def batch_archive(capture: BatchArchive, uid: UID, repo: Repo):
    items = repo.batch_archive(uid, [str(ref_id) for ref_id in capture.ids], capture.archived)
    return Library(items=items)


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
        return search(repo, ai, uid, capture.query,
                      str(capture.space_id) if capture.space_id else None)
    except (AIError, FailedPrecondition, DeadlineExceeded, ServiceUnavailable) as exc:
        raise HTTPException(
            503, "A busca está temporariamente indisponível. Tente novamente.",
        ) from exc
