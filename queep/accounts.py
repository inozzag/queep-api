"""Durable account erasure, resumed by the private maintenance job."""

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import HTTPException
from firebase_admin import auth
from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.account_state import deletion_document, require_open
from queep.models import OnboardingProfile
from queep.config import firebase_app
from queep.images import Images, prepare
from queep.repository import Repository
from queep.usage import inventory, policy, reserve_storage

logger = logging.getLogger(__name__)


class Accounts:
    SETTLE_SECONDS = 600  # Longer than an already accepted upload/worker request.

    def __init__(self, db, bucket, settings, transport=None):
        self.db, self.bucket, self.settings = db, bucket, settings
        self.repo = Repository(db)
        self.transport = transport

    GRACE_DAYS = 30

    @staticmethod
    def status(record, now=None):
        now = now or datetime.now(UTC)
        record = record or {}
        state = record.get("state", "active")
        if state == "pending":  # Older, irreversible requests.
            state = "deleting"
        deadline = record.get("delete_at")
        return {"status": state, "version": record.get("version", 0),
                "delete_at": deadline,
                "can_restore": state == "scheduled" and deadline is not None and now < deadline}

    def get_status(self, uid, now=None):
        return self.status(deletion_document(self.db, uid).get().to_dict(), now)

    def request(self, uid, now=None, *, immediate=False, version=0, operation_id=None):
        now = now or datetime.now(UTC)
        operation_id = operation_id or uuid4().hex
        doc = deletion_document(self.db, uid)

        @firestore.transactional
        def write(tx):
            current = doc.get(transaction=tx).to_dict() or {}
            if current.get("operation_id") == operation_id:
                return self.status(current, now)
            if current.get("version", 0) != version:
                raise HTTPException(409, "O estado da conta mudou. Atualize e tente novamente.")
            state = current.get("state", "active")
            if state not in ("active", "scheduled"):
                raise HTTPException(409, "A exclusão definitiva já foi iniciada.")
            if state == "scheduled" and (not immediate or now >= current["delete_at"]):
                raise HTTPException(409, "A conta já tem uma exclusão agendada.")
            deadline = now if immediate else now + timedelta(days=self.GRACE_DAYS)
            record = {"uid": uid, "state": "deleting" if immediate else "scheduled",
                      "version": version + 1, "operation_id": operation_id,
                      "requested_at": now, "delete_at": deadline, "retry_after": deadline,
                      "provider_project": self.settings.revenuecat_project_id,
                      "provider_required": bool(self.settings.revenuecat_project_id
                                                or self.settings.revenuecat_key
                                                or self.settings.billing_mode == "required"),
                      "settle_at": now + timedelta(seconds=self.SETTLE_SECONDS)}
            tx.set(doc, record)
            return self.status(record, now)
        return self.repo.transact(write)

    def cancel(self, uid, version, operation_id, now=None):
        now = now or datetime.now(UTC)
        doc = deletion_document(self.db, uid)

        @firestore.transactional
        def write(tx):
            current = doc.get(transaction=tx).to_dict() or {}
            if current.get("operation_id") == operation_id:
                return self.status(current, now)
            if (current.get("version", 0) != version or current.get("state") != "scheduled"
                    or now >= current["delete_at"]):
                raise HTTPException(409, "O prazo de recuperação terminou ou a conta mudou.")
            active = {"state": "active", "version": version + 1,
                      "operation_id": operation_id, "restored_at": now}
            # Keep a version after restoration: delayed requests/work cannot reuse old intent.
            tx.set(doc, active)
            return self.status(active, now)
        return self.repo.transact(write)

    def claim(self, uid, now):
        doc = deletion_document(self.db, uid)

        @firestore.transactional
        def write(tx):
            current = doc.get(transaction=tx).to_dict()
            if not current or current["state"] in ("active", "deleted"):
                return None
            if current["state"] == "scheduled":
                if now < current["delete_at"]:
                    return None
                current.update(state="deleting", version=current.get("version", 0) + 1)
                tx.update(doc, {"state": current["state"], "version": current["version"]})
            return current
        return self.repo.transact(write)

    def erase_customer(self, uid, request):
        if not request.get("provider_required", True):
            return True
        project = request.get("provider_project") or self.settings.revenuecat_project_id
        if not project or project != self.settings.revenuecat_project_id:
            raise RuntimeError("RevenueCat erasure project is not configured")
        key = self.settings.revenuecat_delete_key
        if not key:
            raise RuntimeError("RevenueCat erasure is not configured")
        with httpx.Client(timeout=15, transport=self.transport) as client:
            url = (f"https://api.revenuecat.com/v2/projects/{quote(project, safe='')}/customers/"
                   + quote(uid, safe=""))
            headers = {"Authorization": "Bearer " + key.get_secret_value()}
            current = client.get(url, headers=headers)
            if current.status_code == 404:
                return True
            current.raise_for_status()
            response = client.delete(url, headers=headers)
            if response.status_code == 404:
                return True
            response.raise_for_status()
            # V2 returns 200 when queued. A later GET must confirm absence.
            return False

    def erase_batch(self, query, limit=200):
        records = list(query.limit(limit).stream())
        if records:
            batch = self.db.batch()
            for record in records:
                batch.delete(record.reference)
            batch.commit()
        return len(records) == limit

    def process(self, uid, now=None):
        now = now or datetime.now(UTC)
        doc = deletion_document(self.db, uid)
        request = self.claim(uid, now)
        if not request:
            return self.get_status(uid, now)["status"] in ("active", "deleted")
        try:
            auth.delete_user(uid, app=firebase_app())
        except auth.UserNotFoundError:
            pass
        # Both app environments share account identity; erasure must cover both outboxes.
        more = False
        for name in ("jobs", "jobs_testflight"):
            more = self.erase_batch(self.db.collection(name).where(
                filter=FieldFilter("uid", "==", uid))) or more
        more = self.erase_batch(self.db.collection("space_cleanup").where(
            filter=FieldFilter("uid", "==", uid))) or more
        root = self.db.collection("users").document(uid)
        profile = root.get().to_dict() or {}
        prefixes = set(profile.get("avatar_reservations", {}))
        if profile.get("avatar_prefix"):
            prefixes.add(profile["avatar_prefix"])
        for prefix in prefixes:
            self.delete_avatar_objects(uid, prefix)
        for name in ("references", "usage", "fair_use", "inventory", "spaces"):
            more = self.erase_batch(root.collection(name)) or more
        root.delete()
        self.db.collection("billing_access").document(hashlib.sha256(uid.encode()).hexdigest()).delete()

        uploads = list(self.db.collection("image_uploads").where(
            filter=FieldFilter("uid", "==", uid)).limit(50).stream())
        for snapshot in uploads:
            upload = snapshot.to_dict()
            job_id = self.repo.job(uid, upload["reference_id"]).id
            for prefix in Images.object_prefixes(job_id, upload):
                # Keep the reservation until existing writers have had time to stop.
                for name in ("original", "thumbnail", "card"):
                    try:
                        self.bucket.blob(f"{prefix}/{name}").delete(timeout=20, retry=None)
                    except NotFound:
                        pass
            if request["settle_at"] <= now:
                snapshot.reference.delete()
        more = len(uploads) == 50 or more
        if more or now < request["settle_at"]:
            self.defer(uid, now)
            return False
        @firestore.transactional
        def finish(tx):
            current = doc.get(transaction=tx).to_dict()
            if current and current["state"] in ("pending", "deleting"):
                if current.get("provider_required", True):
                    # Vendor cleanup is independent and explicitly deferred for this release.
                    provider = self.db.collection("provider_deletions").document(doc.id)
                    tx.set(provider, {"uid": uid, "state": "pending", "requested_at": now,
                                      "provider_project": current.get("provider_project", "")})
                tx.set(doc, {"state": "deleted", "completed_at": now,
                             "version": current.get("version", 0),
                             "operation_id": current.get("operation_id")})
        self.repo.transact(finish)
        return True

    def defer(self, uid, now):
        doc = deletion_document(self.db, uid)

        @firestore.transactional
        def write(tx):
            current = doc.get(transaction=tx).to_dict()
            if current and current["state"] in ("pending", "deleting"):
                tx.update(doc, {"retry_after": now + timedelta(minutes=5)})
        self.repo.transact(write)

    def resume(self, uid, now=None):
        now = now or datetime.now(UTC)
        try:
            return self.process(uid, now)
        except Exception:
            # No private identifiers, credentials or vendor responses in logs.
            logger.warning("account_deletion_deferred")
            self.defer(uid, now)
            return False

    def sweep(self, limit=20):
        now = datetime.now(UTC)
        pending = self.db.collection("account_deletions").where(
            filter=FieldFilter("retry_after", "<=", now)).order_by("retry_after").limit(limit)
        return sum(self.resume(doc.to_dict()["uid"], now) for doc in pending.stream())

    def avatar_key(self, uid):
        return hashlib.sha256(uid.encode()).hexdigest()

    def avatar_prefix(self, uid, sha256):
        return f"avatars/v1/{self.avatar_key(uid)}/{sha256}"

    def validate_avatar_prefix(self, uid, prefix):
        expected = f"avatars/v1/{self.avatar_key(uid)}/"
        rest = prefix[len(expected):] if prefix.startswith(expected) else ""
        if not rest or not re.fullmatch(r"[a-f0-9]{64}", rest):
            raise RuntimeError("Invalid avatar prefix")

    def put_object(self, path, data, content_type):
        blob = self.bucket.blob(path)
        blob.cache_control = "private, no-store"
        try:
            blob.upload_from_string(
                data, content_type=content_type, if_generation_match=0,
                timeout=30, retry=None, checksum="crc32c")
        except PreconditionFailed:
            pass

    def delete_avatar_objects(self, uid, prefix):
        self.validate_avatar_prefix(uid, prefix)
        for name in ("original", "thumbnail"):
            try:
                self.bucket.blob(f"{prefix}/{name}").delete(timeout=20, retry=None)
            except NotFound:
                pass

    def put_avatar(self, uid, data):
        image = prepare(data)
        prefix = self.avatar_prefix(uid, image.info.sha256)
        require_open(self.db, uid)
        access = policy(self.db, uid)
        root = self.db.collection("users").document(uid)
        size = len(image.original) + len(image.thumbnail)

        @firestore.transactional
        def reserve(tx):
            require_open(self.db, uid, tx)
            current = root.get(transaction=tx).to_dict() or {}
            reservations = current.get("avatar_reservations", {})
            if prefix in reservations:
                return
            reserve_storage(self.db, uid, tx, access, size, upload=True)
            reservations = {**reservations, prefix: size}
            tx.set(root, {"avatar_reservations": reservations,
                          "avatar_storage_bytes": sum(reservations.values())}, merge=True)
        self.repo.transact(reserve)
        try:
            for name, content, content_type in (
                ("original", image.original, image.info.content_type),
                ("thumbnail", image.thumbnail, image.info.content_type),
            ):
                self.put_object(f"{prefix}/{name}", content, content_type)
        except GoogleAPICallError as error:
            raise HTTPException(503, "Upload não confirmado. Tente novamente") from error
        root = self.db.collection("users").document(uid)
        now = datetime.now(UTC)
        previous = {"prefix": None}

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            current = root.get(transaction=tx).to_dict() or {}
            previous["prefix"] = current.get("avatar_prefix")
            tx.set(root, {"avatar": image.info.model_dump(), "avatar_prefix": prefix,
                          "avatar_updated_at": now}, merge=True)
            return image.info.model_dump()

        result = self.repo.transact(write)
        old = previous["prefix"]
        if old and old != prefix:
            self.delete_avatar_objects(uid, old)
            self.release_avatar(uid, old)
        return result

    def get_avatar(self, uid, thumbnail=False):
        require_open(self.db, uid)
        record = self.db.collection("users").document(uid).get().to_dict() or {}
        info = record.get("avatar") or {}
        prefix = record.get("avatar_prefix")
        if not info or not prefix:
            return None
        self.validate_avatar_prefix(uid, prefix)
        name = "thumbnail" if thumbnail else "original"
        try:
            data = self.bucket.blob(f"{prefix}/{name}").download_as_bytes(timeout=20, retry=None)
        except NotFound as error:
            raise HTTPException(404, "Foto de perfil não encontrada") from error
        except GoogleAPICallError as error:
            raise HTTPException(503, "Não foi possível carregar a foto") from error
        return data, info.get("content_type", "image/jpeg")

    def delete_avatar(self, uid):
        root = self.db.collection("users").document(uid)
        previous = {"prefix": None}

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            current = root.get(transaction=tx).to_dict() or {}
            previous["prefix"] = current.get("avatar_prefix")
            if not current:
                return
            tx.set(root, {"avatar": firestore.DELETE_FIELD, "avatar_prefix": firestore.DELETE_FIELD,
                          "avatar_updated_at": firestore.DELETE_FIELD}, merge=True)

        self.repo.transact(write)
        if prefix := previous["prefix"]:
            self.delete_avatar_objects(uid, prefix)
            self.release_avatar(uid, prefix)

    def release_avatar(self, uid, prefix):
        root = self.db.collection("users").document(uid)

        @firestore.transactional
        def release(tx):
            require_open(self.db, uid, tx)
            current = root.get(transaction=tx).to_dict() or {}
            reservations = dict(current.get("avatar_reservations", {}))
            size = reservations.pop(prefix, 0)
            if not size:
                return
            stock, stored = inventory(self.db, uid, tx)
            tx.set(stock, {**stored, "storage_bytes": max(0, stored["storage_bytes"] - size)})
            tx.set(root, {"avatar_reservations": reservations,
                          "avatar_storage_bytes": sum(reservations.values())}, merge=True)
        self.repo.transact(release)

    def get_onboarding(self, uid):
        require_open(self.db, uid)
        record = self.db.collection("users").document(uid).get().to_dict() or {}
        return record.get("onboarding")

    def save_onboarding(self, uid, profile: OnboardingProfile):
        require_open(self.db, uid)
        now = datetime.now(UTC)
        stored = {**profile.model_dump(), "completed_at": now}
        self.db.collection("users").document(uid).set({"onboarding": stored}, merge=True)
        return stored
