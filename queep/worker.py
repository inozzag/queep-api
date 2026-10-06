"""Private Cloud Run entrypoint. Tasks contain identifiers, never reference content."""

import logging
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict, Field

from queep.ai import AI, AIError, document, generated_title, headline_title, without_product_prefix
from queep.budget import Budget
from queep.config import Settings, database, firebase_app, image_bucket
from queep.cost_context import attribution
from queep.dispatch import enqueue
from queep.images import Images, as_stored_image
from queep.jobs import Jobs
from queep.media import MediaFiles, evidence, social_evidence
from queep.models import Interpretation
from queep.social import is_profile, owner
from queep.sources import (
    PHOTO_HOSTS,
    VIDEO_HOSTS,
    SourceError,
    checked_url,
    read_link,
    read_photo,
    read_public_clip,
    read_public_image,
    read_social_clip,
    tweet_status_id,
)

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


def library_title(ref, analysis, frames):
    """Native network title wins; otherwise the AI names posts that have text or media."""
    if (ref.title or "").strip():
        return None
    if ref.kind != "link":
        return None
    if ref.url:
        try:
            if is_profile(checked_url(ref.url)):
                return None
        except SourceError:
            return None
    if ref.source and ref.source.native_title and (ref.source.title or "").strip():
        return ref.source.title.strip()[:200]
    has_material = bool(frames) or bool((ref.transcript or "").strip())
    if ref.source and (ref.source.text or "").strip():
        has_material = True
    if not has_material:
        return None
    page = bool(ref.source and ref.source.content_kind == "page")
    title = generated_title(analysis, words=12 if page else 6) or None
    if title and page:
        title = headline_title(
            without_product_prefix(title, ref.source.title), words=12
        ) or None
    return title


@lru_cache
def engine():
    settings = Settings()
    if not settings.ai_enabled:
        raise HTTPException(503, "Processamento de IA indisponível")
    return AI(settings.openrouter_key.get_secret_value(),
              Budget(database(), settings.ai_daily_budget, settings.search_daily_limit))


def as_previews(raw):
    if not raw:
        return []
    if isinstance(raw, str):
        return [{"url": raw, "kind": "photo"}]
    items = []
    for item in raw:
        if isinstance(item, str):
            items.append({"url": item, "kind": "photo"})
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            kind = item["kind"] if item.get("kind") in ("photo", "video", "avatar") else "photo"
            entry = {"url": item["url"], "kind": kind}
            if kind == "video" and isinstance(item.get("clip_url"), str):
                entry["clip_url"] = item["clip_url"]
            items.append(entry)
    return items[:5]


def load_photo(url):
    """Public CDN or site image bytes; convert ICO/WEBP so attach_preview can store JPEG."""
    delay = 0.4
    for attempt in range(3):
        try:
            host = checked_url(url).host
            data = read_photo(url) if host in PHOTO_HOSTS else read_public_image(url)
            return as_stored_image(data)
        except SourceError as error:
            if str(error) != "transient" or attempt == 2:
                raise
            time.sleep(delay)
            delay *= 2


def load_clip(url):
    host = checked_url(url).host
    return read_social_clip(url) if host in VIDEO_HOSTS else read_public_clip(url)


def process(task, jobs, ai, images, reader=read_link, media=None):
    # Each retry and each paid call carries the same logical operation key.
    record = jobs.repo.jobs().document(task["job_id"]).get().to_dict()
    if not record:
        return "ignored"
    plan = record.get("plan", "unknown")
    reference = (jobs.repo.collection(record["uid"]).document(record["reference_id"])
                 .get().to_dict() or {})
    kind = ("media" if reference.get("kind") in ("audio", "video")
            else reference.get("kind", "unknown"))
    if kind == "link" and reference.get("url"):
        try:
            if owner(checked_url(reference["url"])):
                kind = "social"
        except SourceError:
            pass
    with attribution(plan=plan, kind=kind, operation_id=record.get("run_id", task["run_id"]),
                     media_seconds=(reference.get("media") or {}).get("duration_seconds", 0)):
        return process_work(task, jobs, ai, images, reader, media)


def process_work(task, jobs, ai, images, reader=read_link, media=None):
    work = jobs.claim(task)
    if not work:
        return "ignored"
    try:
        ref = work["reference"]
        data = images.read(work["uid"], ref.id)[0] if images and ref.kind == "image" else None
        if not jobs.current(work):
            return "stale"
        frames = []
        profile = False
        page = False
        if ref.kind == "link":
            fetched = reader(ref.url)
            source, bundle = (fetched if isinstance(fetched, tuple) else (fetched, None))
            try:
                profile = bool(ref.url and is_profile(checked_url(ref.url)))
            except SourceError:
                profile = False
            if not bundle and ref.url:
                try:
                    if tweet_status_id(checked_url(ref.url)):
                        time.sleep(0.6)
                        fetched = reader(ref.url)
                        retry_source, retry_bundle = (
                            fetched if isinstance(fetched, tuple) else (fetched, None)
                        )
                        if retry_bundle:
                            source, bundle = retry_source, retry_bundle
                except SourceError:
                    pass
            ref = ref.model_copy(update={"source": source})
            page = bool(source and source.content_kind == "page")
            all_previews = as_previews(bundle)
            author = next((item for item in all_previews if item["kind"] == "avatar"), None)
            previews = [] if profile else [
                item for item in all_previews if item["kind"] in ("photo", "video")
            ][:4]
            infos = []
            if author and images:
                try:
                    photo = load_photo(author["url"])
                    avatar = images.attach_author(work["uid"], ref.id, photo)
                    ref = ref.model_copy(update={"author_image": avatar})
                except Exception:
                    logger.warning("post_author_skipped", extra={"uid": work["uid"]}, exc_info=True)
            if previews and images:
                for preview in previews:
                    try:
                        photo = load_photo(preview["url"])
                        info = images.attach_preview(
                            work["uid"], ref.id, photo, preview_kind=preview["kind"])
                        infos.append(info)
                        if not page:
                            frames.append(photo)
                    except Exception:
                        logger.warning("post_photo_skipped", extra={"count": len(infos)},
                                       exc_info=True)
                logger.info("post_photos_attached %s/%s", len(infos), len(previews))
                if infos:
                    ref = ref.model_copy(update={"image": infos[0], "images": infos})
            clip_url = None if profile or page else next(
                (item.get("clip_url") for item in previews if item.get("clip_url")), None)
            if clip_url:
                try:
                    clip = social_evidence(load_clip(clip_url))
                    frames.extend(frame for _, frame in clip.frames)
                    if clip.audio:
                        speech = ai.transcribe_speech(clip.audio)
                        if speech:
                            ref = ref.model_copy(update={"transcript": speech})
                except Exception:
                    logger.warning("post_clip_skipped", extra={"uid": work["uid"]}, exc_info=True)
            if not jobs.current(work):
                return "stale"
        transcript = ref.transcript
        if ref.media:
            files = media or MediaFiles(images.db, images.bucket)
            content = evidence(files.read(work["uid"], ref.id)[0], ref.media)
            if not jobs.current(work):
                return "stale"
            analysis, transcript = ai.interpret_media(ref, content)
            ref = ref.model_copy(update={"transcript": transcript})
        elif profile and not (ref.source and (ref.source.text or "").strip()):
            analysis = Interpretation(
                summary="",
                coverage="partial",
                limitations=["O perfil não tem bio pública para analisar."],
            )
        else:
            analysis = ai.interpret(ref, None if profile or page else (frames or data))
        title = library_title(ref, analysis, frames)
        if title:
            ref = ref.model_copy(update={"title": title})
        if not jobs.current(work):
            return "stale"
        vector = ai.embed(document(ref, analysis))
        return "ready" if jobs.finish(work, analysis=analysis, vector=vector,
                                       source=ref.source, transcript=transcript,
                                       title=title, image=ref.image, images=ref.images,
                                       author_image=ref.author_image) else "stale"
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
    from queep.spaces import Spaces
    try:
        Spaces(database()).sweep()
    except Exception:
        logger.warning("space_cleanup_failed", exc_info=True)
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
