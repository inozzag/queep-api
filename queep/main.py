from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth

from queep.config import Settings, database, firebase_app, image_bucket
from queep.images import MAX_BYTES, Images
from queep.models import Capture, Edit, Library, Reference
from queep.repository import Repository


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
        return claims["uid"]
    except (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError,
            auth.UserNotFoundError, ValueError) as exc:
        raise HTTPException(401, "Sessão inválida", headers={"WWW-Authenticate": "Bearer"}) from exc


UID = Annotated[str, Depends(user)]


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


@app.put("/v1/references/{ref_id}", response_model=Reference)
def create(ref_id: UUID, capture: Capture, uid: UID, repo: Repo):
    return repo.save(uid, str(ref_id), capture)


@app.put("/v1/references/{ref_id}/image", response_model=Reference, openapi_extra={
    "requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    }},
})
async def upload_image(ref_id: UUID, request: Request, uid: UID, media: Media,
                       title: Annotated[str, Query(max_length=200)] = ""):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "Escolha uma imagem de até 12 MB")
        data.extend(chunk)
    return await anyio.to_thread.run_sync(media.upload, uid, str(ref_id), title.strip(),
                                         bytes(data), limiter=image_limiter)


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
def edit(ref_id: UUID, capture: Edit, uid: UID, repo: Repo):
    return repo.save(uid, str(ref_id), capture, editing=True)


@app.delete("/v1/references/{ref_id}", status_code=204)
def delete(ref_id: UUID, uid: UID, repo: Repo, media: Media,
           revision: Annotated[int, Query(ge=1)]):
    repo.delete(uid, str(ref_id), revision)
    media.cleanup(uid, str(ref_id))
    return Response(status_code=204)
