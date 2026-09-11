from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth

from queep.config import Settings, database, firebase_app
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
def delete(ref_id: UUID, uid: UID, repo: Repo, revision: Annotated[int, Query(ge=1)]):
    repo.delete(uid, str(ref_id), revision)
    return Response(status_code=204)
