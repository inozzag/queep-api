"""Immutable images with a durable reservation spanning Firestore and Storage."""

import hashlib
import logging
import re
import threading
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO

from fastapi import HTTPException
from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from PIL import Image, ImageOps, UnidentifiedImageError

from queep.account_state import is_closed, require_open
from queep.models import ImageInfo, Reference
from queep.repository import Repository, fingerprint, new_job
from queep.usage import check_inventory, inventory, policy

MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_GALLERY = 4
_KIND_RANK = {"photo": 0, "gif": 1, "video": 1}
CARD_EDGE = 1440
SHARP_EDGE = 800
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
logger = logging.getLogger(__name__)
_preview_cache: OrderedDict[str, bytes] = OrderedDict()
_preview_lock = threading.Lock()
_preview_slots = threading.BoundedSemaphore(2)
_byte_cache: OrderedDict[str, bytes] = OrderedDict()
_BYTE_BUDGET = 48 * 1024 * 1024
STORED_OBJECTS = ("original", "thumbnail", "card", "motion", "loop")


def richer_preview(current, incoming):
    """Replace a stored still when the same bytes now have motion or a video kind."""
    if not isinstance(current, dict):
        return None
    current_kind = current.get("preview_kind") or "photo"
    motion_up = bool(incoming.motion) and not current.get("motion")
    kind_up = _KIND_RANK.get(incoming.preview_kind, 0) > _KIND_RANK.get(current_kind, 0)
    if not motion_up and not kind_up:
        return None
    return {**current, **incoming.model_dump()}


@dataclass
class PreparedImage:
    original: bytes
    thumbnail: bytes
    info: ImageInfo


def prepare(data: bytes) -> PreparedImage:
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "Escolha uma imagem de até 12 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG"} or getattr(source, "n_frames", 1) != 1:
                    raise HTTPException(415, "Envie uma imagem estática JPEG ou PNG")
                if source.width * source.height > MAX_PIXELS:
                    raise HTTPException(413, "A imagem tem resolução muito alta")
                source.load()  # Validate the entire file, not its extension or MIME header.
                oriented = ImageOps.exif_transpose(source)
                transparent = "A" in oriented.getbands() or "transparency" in oriented.info
                clean = oriented.convert("RGBA" if transparent else "RGB")
                clean.info.clear()  # No EXIF, GPS, text chunks or embedded thumbnails.
                clean.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                format_name = "PNG" if transparent else "JPEG"

                def encode(value, quality):
                    buffer = BytesIO()
                    value.save(buffer, format=format_name, quality=quality)
                    return buffer.getvalue()

                original = encode(clean, 92)
                width, height = clean.size
                clean.thumbnail((CARD_EDGE, CARD_EDGE), Image.Resampling.LANCZOS)
                thumbnail = encode(clean, 84)
        if len(original) > MAX_BYTES:
            raise HTTPException(413, "A imagem é muito grande. Reduza a resolução")
        return PreparedImage(
            original,
            thumbnail,
            ImageInfo(
                sha256=hashlib.sha256(data).hexdigest(),
                content_type="image/png" if transparent else "image/jpeg",
                width=width,
                height=height,
                byte_count=len(original),
            ),
        )
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise HTTPException(415, "Não foi possível ler esta imagem. Escolha JPEG ou PNG") from error


def recall_bytes(key: str) -> bytes | None:
    with _preview_lock:
        cached = _byte_cache.get(key)
        if cached is not None:
            _byte_cache.move_to_end(key)
        return cached


def remember_bytes(key: str, data: bytes) -> None:
    """Keep small private derivatives in this process so the next gallery paint skips Storage."""
    if not data or len(data) > 2 * 1024 * 1024:
        return
    with _preview_lock:
        _byte_cache[key] = data
        _byte_cache.move_to_end(key)
        total = sum(len(item) for item in _byte_cache.values())
        while total > _BYTE_BUDGET and len(_byte_cache) > 1:
            _, dropped = _byte_cache.popitem(last=False)
            total -= len(dropped)


def animated_gif(data: bytes) -> bool:
    if not data.startswith((b"GIF87a", b"GIF89a")):
        return False
    try:
        with Image.open(BytesIO(data)) as image:
            return getattr(image, "n_frames", 1) > 1
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def preview_edge(data: bytes) -> int:
    try:
        with Image.open(BytesIO(data)) as image:
            return max(image.size)
    except (UnidentifiedImageError, OSError, ValueError):
        return 0


def jpeg_card(data: bytes, edge: int = CARD_EDGE) -> bytes:
    """A display-sized JPEG. Gallery cards need more than the legacy 560 px preview."""
    with Image.open(BytesIO(data)) as image:
        image.load()
        frame = ImageOps.exif_transpose(image)
        if "A" in frame.getbands():
            background = Image.new("RGB", frame.size, "white")
            background.paste(frame, mask=frame.getchannel("A"))
            frame = background
        else:
            frame = frame.convert("RGB")
        frame.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        frame.save(buffer, format="JPEG", quality=84, optimize=True)
        return buffer.getvalue()


def cached_preview(key: str, build) -> bytes | None:
    with _preview_lock:
        cached = _preview_cache.get(key)
        if cached is not None:
            _preview_cache.move_to_end(key)
            return cached
    if not _preview_slots.acquire(timeout=12):
        return None
    try:
        with _preview_lock:
            cached = _preview_cache.get(key)
            if cached is not None:
                _preview_cache.move_to_end(key)
                return cached
        data = build()
        if not data:
            return None
        with _preview_lock:
            _preview_cache[key] = data
            _preview_cache.move_to_end(key)
            while len(_preview_cache) > 24:
                _preview_cache.popitem(last=False)
        return data
    finally:
        _preview_slots.release()


def as_jpeg(data: bytes) -> bytes:
    with Image.open(BytesIO(data)) as source:
        source.load()
        rgb = ImageOps.exif_transpose(source).convert("RGB")
        buffer = BytesIO()
        rgb.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()


def as_stored_image(data: bytes) -> bytes:
    """Keep JPEG/PNG; convert ICO/WEBP so prepare() can store the preview."""
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format in {"JPEG", "PNG"} and getattr(source, "n_frames", 1) == 1:
                return data
        return as_jpeg(data)
    except (UnidentifiedImageError, OSError, ValueError):
        return data


class Images:
    prepare = staticmethod(prepare)

    def kind(self, image):
        return "image"

    def fields(self, image):
        return {"image": image.info.model_dump()}

    def objects(self, image):
        return [("original", image.original, image.info.content_type),
                ("thumbnail", image.thumbnail, image.info.content_type)]

    def __init__(self, db, bucket):
        self.repo = Repository(db)
        self.db = db
        self.bucket = bucket

    def manifest(self, uid, ref_id):
        return self.db.collection("image_uploads").document(self.repo.job(uid, ref_id).id)

    def prefix(self, uid, ref_id, sha256):
        return f"images/v1/{self.repo.job(uid, ref_id).id}/{sha256}"

    @staticmethod
    def object_prefixes(job_id, upload):
        expected = f"images/v1/{job_id}/"
        prefixes = list(upload.get("prefixes") or [])
        if upload.get("prefix") and upload["prefix"] not in prefixes:
            prefixes.append(upload["prefix"])
        for prefix in prefixes:
            rest = prefix[len(expected):]
            if not prefix.startswith(expected) or not re.fullmatch(r"[a-f0-9]{64}", rest):
                raise RuntimeError("Invalid image reservation")
        return prefixes

    def reserve(self, uid, ref_id, title, image, space_ids=()):
        access = policy(self.db, uid)
        doc = self.repo.collection(uid).document(ref_id)
        manifest = self.manifest(uid, ref_id)
        payload = {"kind": self.kind(image), "title": title, "sha256": image.info.sha256}
        if space_ids:
            payload["space_ids"] = sorted(set(space_ids))
        digest = fingerprint(payload)
        now = datetime.now(UTC)

        @firestore.transactional
        def begin(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            upload = manifest.get(transaction=tx).to_dict()
            if old and not old.get("uploading"):
                if not old.get("deleted") and old.get("create_hash") == digest:
                    return Reference.model_validate(old)
                raise HTTPException(409, "Identificador de envio já utilizado")
            if upload and (upload["state"] != "pending" or upload["digest"] != digest):
                raise HTTPException(409, "Identificador de envio já utilizado")
            from queep.spaces import Spaces
            Spaces(self.db).require(uid, space_ids, tx)
            if upload:
                inventory(self.db, uid, tx)  # Honor a reconciliation lock on resumed uploads too.
            if not upload:
                from queep.usage import reserve_operation
                reserve_operation(self.db, uid, tx, now=now, access=access, items=1,
                                  upload_bytes=sum(len(data) for _, data, _ in self.objects(image)),
                                  media_seconds=getattr(image.info, "duration_seconds", 0))
            # Hidden placeholder also reserves the ID against simultaneous note/link creation.
            tx.set(doc, {"deleted": True, "uploading": True, "revision": 0, "updated_at": now})
            tx.set(
                manifest,
                {
                    "uid": uid,
                    "reference_id": ref_id,
                    "digest": digest,
                    "state": "pending",
                    "prefix": self.prefix(uid, ref_id, image.info.sha256),
                    "cleanup_after": now + timedelta(hours=1),
                    "settle_at": now + timedelta(minutes=10),
                    "storage_bytes": sum(len(data) for _, data, _ in self.objects(image)),
                    "plan": access.plan,
                },
            )
            return None

        return digest, self.repo.transact(begin)

    def put_object(self, path, data, content_type):
        blob = self.bucket.blob(path)
        blob.cache_control = "private, no-store"
        try:
            # Never overwrite an object another retry has already confirmed.
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=0,
                timeout=30,
                retry=None,
                checksum="crc32c",
            )
        except PreconditionFailed:
            pass

    def upload(self, uid, ref_id, title, data, space_ids=()):
        image = self.prepare(data)
        digest, existing = self.reserve(uid, ref_id, title, image, space_ids)
        if existing:
            return existing
        prefix = self.prefix(uid, ref_id, image.info.sha256)
        try:
            for name, content, content_type in self.objects(image):
                self.put_object(f"{prefix}/{name}", content, content_type)
        except GoogleAPICallError as error:
            # Reservation remains retryable and is swept if abandoned.
            raise HTTPException(503, "Upload não confirmado. Tente novamente") from error
        doc = self.repo.collection(uid).document(ref_id)
        manifest = self.manifest(uid, ref_id)
        now = datetime.now(UTC)

        @firestore.transactional
        def finish(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            upload = manifest.get(transaction=tx).to_dict()
            if old and not old.get("deleted") and old.get("create_hash") == digest:
                return old
            if (
                not old
                or not old.get("uploading")
                or not upload
                or upload["state"] != "pending"
                or upload["digest"] != digest
            ):
                raise HTTPException(409, "Este envio foi encerrado")
            from queep.spaces import Spaces
            Spaces(self.db).require(uid, space_ids, tx)
            record = {
                "id": ref_id,
                "kind": self.kind(image),
                "title": title,
                "text": "",
                "url": None,
                **self.fields(image),
                "deleted": False,
                "revision": 1,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "analysis": None,
                "create_hash": digest,
                "content_hash": digest,
                "last_mutation_id": None,
                "space_ids": sorted(set(space_ids)),
            }
            tx.set(doc, record)
            tx.set(
                self.repo.job(uid, ref_id),
                new_job(uid, ref_id, 1, now) | {"plan": upload.get("plan", "unknown")},
            )
            tx.update(manifest, {"state": "active", "cleanup_after": firestore.DELETE_FIELD})
            return record

        try:
            return Reference.model_validate(self.repo.transact(finish))
        except HTTPException as error:
            if error.status_code == 409:
                self.cleanup(uid, ref_id)  # Also handles a late writer racing deletion/expiry.
            raise

    def attach_preview(self, uid, ref_id, data, preview_kind="photo", motion=None):
        """Store a public post photo, GIF, or video poster without changing the link kind."""
        kind = preview_kind if preview_kind in ("photo", "video", "gif") else "photo"
        motion_bytes = motion if isinstance(motion, bytes) else None
        if motion_bytes is None or not 32 <= len(motion_bytes) <= 4 * 1024 * 1024:
            motion_bytes = None
        if kind == "gif" and not motion_bytes:
            kind = "photo"
        try:
            image = self.prepare(data)
        except HTTPException:
            image = self.prepare(as_jpeg(data))
        info = image.info.model_copy(update={"preview_kind": kind, "motion": bool(motion_bytes)})
        prefix = self.prefix(uid, ref_id, image.info.sha256)
        self.reserve_preview(uid, ref_id, image, extra=len(motion_bytes or b""))
        for name, content, content_type in self.objects(image):
            self.put_object(f"{prefix}/{name}", content, content_type)
        if motion_bytes:
            self.put_object(f"{prefix}/motion", motion_bytes, "video/mp4")
        now = datetime.now(UTC)
        manifest = self.manifest(uid, ref_id)
        doc = self.repo.collection(uid).document(ref_id)

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            record = doc.get(transaction=tx).to_dict()
            if not record or record.get("deleted") or record.get("kind") != "link":
                raise HTTPException(409, "Referência indisponível")
            gallery = list(record.get("images") or [])
            if not gallery and record.get("image"):
                gallery = [record["image"]]
            for index, item in enumerate(gallery):
                if item.get("sha256") != image.info.sha256:
                    continue
                upgraded = richer_preview(item, info)
                if upgraded is None:
                    return ImageInfo.model_validate(item)
                gallery[index] = upgraded
                tx.update(doc, {"image": gallery[0], "images": gallery, "updated_at": now})
                return ImageInfo.model_validate(upgraded)
            if len(gallery) >= MAX_GALLERY:
                return ImageInfo.model_validate(gallery[-1])
            gallery.append(info.model_dump())
            upload = manifest.get(transaction=tx).to_dict()
            prefixes = list((upload or {}).get("prefixes") or [])
            if (upload or {}).get("prefix") and upload["prefix"] not in prefixes:
                prefixes.append(upload["prefix"])
            if prefix not in prefixes:
                prefixes.append(prefix)
            if not upload:
                tx.set(manifest, {
                    "uid": uid,
                    "reference_id": ref_id,
                    "digest": image.info.sha256,
                    "state": "active",
                    "prefix": prefixes[0],
                    "prefixes": prefixes,
                    "settle_at": now + timedelta(minutes=10),
                })
            else:
                tx.update(manifest, {"prefix": prefixes[0], "prefixes": prefixes})
            tx.update(doc, {"image": gallery[0], "images": gallery, "updated_at": now})
            return info

        return self.repo.transact(write)

    def attach_author(self, uid, ref_id, data):
        """Store the public author avatar on a link without mixing it into the post gallery."""
        try:
            image = self.prepare(data)
        except HTTPException:
            image = self.prepare(as_jpeg(data))
        info = image.info
        prefix = self.prefix(uid, ref_id, image.info.sha256)
        self.reserve_preview(uid, ref_id, image)
        for name, content, content_type in self.objects(image):
            self.put_object(f"{prefix}/{name}", content, content_type)
        now = datetime.now(UTC)
        manifest = self.manifest(uid, ref_id)
        doc = self.repo.collection(uid).document(ref_id)

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            record = doc.get(transaction=tx).to_dict()
            if not record or record.get("deleted") or record.get("kind") != "link":
                raise HTTPException(409, "Referência indisponível")
            current = record.get("author_image")
            if current and current.get("sha256") == image.info.sha256:
                return ImageInfo.model_validate(current)
            upload = manifest.get(transaction=tx).to_dict()
            prefixes = list((upload or {}).get("prefixes") or [])
            if (upload or {}).get("prefix") and upload["prefix"] not in prefixes:
                prefixes.append(upload["prefix"])
            if prefix not in prefixes:
                prefixes.append(prefix)
            if not upload:
                tx.set(manifest, {
                    "uid": uid,
                    "reference_id": ref_id,
                    "digest": image.info.sha256,
                    "state": "active",
                    "prefix": prefixes[0],
                    "prefixes": prefixes,
                    "settle_at": now + timedelta(minutes=10),
                })
            else:
                tx.update(manifest, {"prefix": prefixes[0], "prefixes": prefixes})
            tx.update(doc, {"author_image": info.model_dump(), "updated_at": now})
            return info

        return self.repo.transact(write)

    def reserve_preview(self, uid, ref_id, image, extra=0):
        """Reserve every live object (including social thumbnails) before Storage writes."""
        access = policy(self.db, uid)
        manifest = self.manifest(uid, ref_id)
        prefix = self.prefix(uid, ref_id, image.info.sha256)
        size = sum(len(data) for _, data, _ in self.objects(image)) + max(0, extra)

        @firestore.transactional
        def reserve(tx):
            require_open(self.db, uid, tx)
            record = self.repo.collection(uid).document(ref_id).get(transaction=tx).to_dict()
            old = manifest.get(transaction=tx).to_dict() or {}
            if (not record or record.get("deleted") or record.get("kind") != "link"
                    or old.get("state", "active") != "active"):
                raise HTTPException(409, "Referência indisponível")
            prefixes = list(old.get("prefixes") or [])
            if old.get("prefix") and old["prefix"] not in prefixes:
                prefixes.append(old["prefix"])
            stock, stored = inventory(self.db, uid, tx)
            if prefix in prefixes:
                tx.update(manifest, {"settle_at": datetime.now(UTC) + timedelta(minutes=10)})
                return
            check_inventory(access, stored, storage_bytes=size)
            tx.set(stock, {**stored, "storage_bytes": stored.get("storage_bytes", 0) + size})
            prefixes.append(prefix)
            tx.set(manifest, {**old, "uid": uid, "reference_id": ref_id,
                              "state": "active", "digest": old.get("digest", image.info.sha256),
                              "prefix": prefixes[0], "prefixes": prefixes,
                              "storage_bytes": old.get("storage_bytes", 0) + size,
                              "settle_at": datetime.now(UTC) + timedelta(minutes=10)})
        self.repo.transact(reserve)

    def read(self, uid, ref_id, thumbnail=False, index=0, role="gallery", motion=False):
        reference = self.repo.get(uid, ref_id)
        if role == "author":
            info = reference.author_image
            if not info:
                raise HTTPException(404, "Imagem não encontrada")
        else:
            gallery = reference.images or ([reference.image] if reference.image else [])
            if not gallery or index < 0 or index >= len(gallery):
                raise HTTPException(404, "Imagem não encontrada")
            info = gallery[index]
        prefix = self.prefix(uid, ref_id, info.sha256)
        if motion:
            if role == "author" or not info.motion:
                raise HTTPException(404, "Imagem não encontrada")
            return self._download(f"{prefix}/motion"), "video/mp4"
        name = "thumbnail" if thumbnail else "original"
        data = self._download(f"{prefix}/{name}")
        needs_card = (
            thumbnail and role != "author"
            and max(info.width, info.height) > SHARP_EDGE
            and preview_edge(data) < SHARP_EDGE
        )
        if needs_card:
            sharp = self._card(prefix)
            if sharp:
                return sharp, "image/jpeg"
        return data, info.content_type

    def _download(self, path):
        cached = recall_bytes(path)
        if cached is not None:
            return cached
        try:
            data = self.bucket.blob(path).download_as_bytes(timeout=20, retry=None)
        except NotFound as error:
            raise HTTPException(404, "Imagem não encontrada") from error
        except GoogleAPICallError as error:
            raise HTTPException(503, "Não foi possível carregar a imagem") from error
        remember_bytes(path, data)
        return data

    def _card(self, prefix):
        """Serve a stored 1440 px card. A missing one is built off the request path."""
        card_path = f"{prefix}/card"
        cached = recall_bytes(card_path)
        if cached is not None:
            return cached
        blob = self.bucket.blob(card_path)
        try:
            data = blob.download_as_bytes(timeout=8, retry=None)
        except NotFound:
            self._schedule_card(prefix)
            return None
        except GoogleAPICallError:
            logger.warning("Card preview unavailable", exc_info=True)
            return None
        remember_bytes(card_path, data)
        return data

    def _schedule_card(self, prefix):
        if not _preview_slots.acquire(blocking=False):
            return

        def build():
            try:
                card_path = f"{prefix}/card"
                if recall_bytes(card_path):
                    return
                try:
                    original = self._download(f"{prefix}/original")
                    card = jpeg_card(original)
                except HTTPException:
                    return
                try:
                    self.bucket.blob(card_path).upload_from_string(
                        card, content_type="image/jpeg", timeout=20
                    )
                except GoogleAPICallError:
                    logger.warning("Card preview was not stored", exc_info=True)
                remember_bytes(card_path, card)
            finally:
                _preview_slots.release()

        threading.Thread(target=build, name="queep-card", daemon=True).start()

    def cleanup(self, uid, ref_id, now=None):
        now = now or datetime.now(UTC)
        manifest = self.manifest(uid, ref_id)
        doc = self.repo.collection(uid).document(ref_id)

        @firestore.transactional
        def claim(tx):
            # Account erasure owns these reservations, including the upload settling window.
            if is_closed(self.db, uid, tx):
                return None
            upload = manifest.get(transaction=tx).to_dict()
            if not upload or upload["state"] == "active":
                return None
            if upload["state"] == "pending":
                if upload["cleanup_after"] > now:
                    return None
                # Tombstone blocks future reservations before any object is removed.
                upload.update(state="cleanup", settle_at=now + timedelta(minutes=10))
                stock, stored = inventory(self.db, uid, tx)
                tx.set(stock, {**stored, "items": max(0, stored.get("items", 0) - 1)})
                tx.set(doc, {"deleted": True, "revision": 1, "updated_at": now})
                tx.update(manifest, {"state": "cleanup", "settle_at": upload["settle_at"]})
            return upload

        upload = self.repo.transact(claim)
        if not upload:
            return False
        try:
            for prefix in self.object_prefixes(self.repo.job(uid, ref_id).id, upload):
                for name in STORED_OBJECTS:
                    try:
                        self.bucket.blob(f"{prefix}/{name}").delete(timeout=20, retry=None)
                    except NotFound:
                        pass
        except GoogleAPICallError:
            logger.warning("Image cleanup deferred", exc_info=True)
            return False
        if upload["settle_at"] <= now:
            # After the bounded upload window, the tombstone is sufficient to block recreation.
            @firestore.transactional
            def release(tx):
                current = manifest.get(transaction=tx).to_dict()
                if not current or current["state"] != "cleanup" or current["settle_at"] > now:
                    return
                stock, stored = inventory(self.db, uid, tx)
                tx.set(stock, {**stored, "storage_bytes": max(
                    0, stored.get("storage_bytes", 0) - current.get("storage_bytes", 0))})
                tx.delete(manifest)
            self.repo.transact(release)
        return True

    def sweep(self, limit=200):
        now = datetime.now(UTC)
        uploads = (
            self.db.collection("image_uploads")
            .where(filter=FieldFilter("cleanup_after", "<=", now))
            .order_by("cleanup_after")
            .limit(limit)
            .stream()
        )
        count = 0
        for snapshot in uploads:
            upload = snapshot.to_dict()
            count += self.cleanup(upload["uid"], upload["reference_id"], now)
        return count
