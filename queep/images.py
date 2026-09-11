"""Immutable images with a durable reservation spanning Firestore and Storage."""

import hashlib
import logging
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO

from fastapi import HTTPException
from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from PIL import Image, ImageOps, UnidentifiedImageError

from queep.models import ImageInfo, Reference
from queep.repository import Repository, fingerprint, new_job

MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 20_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
logger = logging.getLogger(__name__)


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
                clean.thumbnail((560, 560), Image.Resampling.LANCZOS)
                thumbnail = encode(clean, 80)
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


class Images:
    def __init__(self, db, bucket):
        self.repo = Repository(db)
        self.db = db
        self.bucket = bucket

    def manifest(self, uid, ref_id):
        return self.db.collection("image_uploads").document(self.repo.job(uid, ref_id).id)

    def prefix(self, uid, ref_id, sha256):
        return f"images/v1/{self.repo.job(uid, ref_id).id}/{sha256}"

    def reserve(self, uid, ref_id, title, image):
        doc = self.repo.collection(uid).document(ref_id)
        manifest = self.manifest(uid, ref_id)
        digest = fingerprint({"kind": "image", "title": title, "sha256": image.info.sha256})
        now = datetime.now(UTC)

        @firestore.transactional
        def begin(tx):
            old = doc.get(transaction=tx).to_dict()
            upload = manifest.get(transaction=tx).to_dict()
            if old and not old.get("uploading"):
                if not old.get("deleted") and old.get("create_hash") == digest:
                    return Reference.model_validate(old)
                raise HTTPException(409, "Identificador de envio já utilizado")
            if upload and (upload["state"] != "pending" or upload["digest"] != digest):
                raise HTTPException(409, "Identificador de envio já utilizado")
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

    def upload(self, uid, ref_id, title, data):
        image = prepare(data)
        digest, existing = self.reserve(uid, ref_id, title, image)
        if existing:
            return existing
        prefix = self.prefix(uid, ref_id, image.info.sha256)
        try:
            self.put_object(f"{prefix}/original", image.original, image.info.content_type)
            self.put_object(f"{prefix}/thumbnail", image.thumbnail, image.info.content_type)
        except GoogleAPICallError as error:
            # Reservation remains retryable and is swept if abandoned.
            raise HTTPException(503, "Upload não confirmado. Tente novamente") from error
        doc = self.repo.collection(uid).document(ref_id)
        manifest = self.manifest(uid, ref_id)
        now = datetime.now(UTC)

        @firestore.transactional
        def finish(tx):
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
            record = {
                "id": ref_id,
                "kind": "image",
                "title": title,
                "text": "",
                "url": None,
                "image": image.info.model_dump(),
                "deleted": False,
                "revision": 1,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "analysis": None,
                "create_hash": digest,
                "content_hash": digest,
                "last_mutation_id": None,
            }
            tx.set(doc, record)
            tx.set(
                self.repo.job(uid, ref_id),
                new_job(uid, ref_id, 1, now),
            )
            tx.update(manifest, {"state": "active", "cleanup_after": firestore.DELETE_FIELD})
            return record

        try:
            return Reference.model_validate(self.repo.transact(finish))
        except HTTPException as error:
            if error.status_code == 409:
                self.cleanup(uid, ref_id)  # Also handles a late writer racing deletion/expiry.
            raise

    def read(self, uid, ref_id, thumbnail=False):
        reference = self.repo.get(uid, ref_id)
        if not reference.image:
            raise HTTPException(404, "Imagem não encontrada")
        name = "thumbnail" if thumbnail else "original"
        path = f"{self.prefix(uid, ref_id, reference.image.sha256)}/{name}"
        try:
            data = self.bucket.blob(path).download_as_bytes(timeout=20, retry=None)
        except NotFound as error:
            raise HTTPException(404, "Imagem não encontrada") from error
        except GoogleAPICallError as error:
            raise HTTPException(503, "Não foi possível carregar a imagem") from error
        return data, reference.image.content_type

    def cleanup(self, uid, ref_id, now=None):
        now = now or datetime.now(UTC)
        manifest = self.manifest(uid, ref_id)
        doc = self.repo.collection(uid).document(ref_id)

        @firestore.transactional
        def claim(tx):
            upload = manifest.get(transaction=tx).to_dict()
            if not upload or upload["state"] == "active":
                return None
            if upload["state"] == "pending":
                if upload["cleanup_after"] > now:
                    return None
                # Tombstone blocks future reservations before any object is removed.
                upload.update(state="cleanup", settle_at=now + timedelta(minutes=10))
                tx.set(doc, {"deleted": True, "revision": 1, "updated_at": now})
                tx.update(manifest, {"state": "cleanup", "settle_at": upload["settle_at"]})
            return upload

        upload = self.repo.transact(claim)
        if not upload:
            return False
        try:
            for name in ("original", "thumbnail"):
                try:
                    self.bucket.blob(f"{upload['prefix']}/{name}").delete(timeout=20, retry=None)
                except NotFound:
                    pass
        except GoogleAPICallError:
            logger.warning("Image cleanup deferred", exc_info=True)
            return False
        if upload["settle_at"] <= now:
            # After the bounded upload window, the tombstone is sufficient to block recreation.
            manifest.delete()
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
