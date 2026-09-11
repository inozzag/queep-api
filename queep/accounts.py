"""Durable account erasure, resumed by the private maintenance job."""

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from firebase_admin import auth
from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.account_state import deletion_document
from queep.config import firebase_app
from queep.repository import Repository

logger = logging.getLogger(__name__)


class Accounts:
    SETTLE_SECONDS = 600  # Longer than an already accepted upload/worker request.

    def __init__(self, db, bucket, settings, transport=None):
        self.db, self.bucket, self.settings = db, bucket, settings
        self.repo = Repository(db)
        self.transport = transport

    def request(self, uid, now=None):
        now = now or datetime.now(UTC)
        doc = deletion_document(self.db, uid)

        @firestore.transactional
        def write(tx):
            if doc.get(transaction=tx).exists:
                return
            tx.create(doc, {"uid": uid, "state": "pending", "requested_at": now,
                            "retry_after": now,
                            "provider_project": self.settings.revenuecat_project_id,
                            "provider_required": bool(self.settings.revenuecat_project_id
                                                      or self.settings.revenuecat_key
                                                      or self.settings.billing_mode == "required"),
                            "settle_at": now + timedelta(seconds=self.SETTLE_SECONDS)})
        self.repo.transact(write)

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
        request = doc.get().to_dict()
        if not request or request["state"] == "deleted":
            return True
        try:
            auth.delete_user(uid, app=firebase_app())
        except auth.UserNotFoundError:
            pass
        more = self.erase_batch(self.db.collection("jobs").where(
            filter=FieldFilter("uid", "==", uid)))
        root = self.db.collection("users").document(uid)
        for name in ("references", "usage"):
            more = self.erase_batch(root.collection(name)) or more
        root.delete()
        self.db.collection("billing_access").document(hashlib.sha256(uid.encode()).hexdigest()).delete()

        uploads = list(self.db.collection("image_uploads").where(
            filter=FieldFilter("uid", "==", uid)).limit(50).stream())
        for snapshot in uploads:
            upload = snapshot.to_dict()
            expected = f"images/v1/{self.repo.job(uid, upload['reference_id']).id}/"
            prefix = upload["prefix"]
            if (not prefix.startswith(expected)
                    or not re.fullmatch(r"[a-f0-9]{64}", prefix[len(expected):])):
                raise RuntimeError("Invalid image reservation")
            # Keep the reservation until existing writers have had time to stop.
            for name in ("original", "thumbnail"):
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
        # Done only after the external processor confirms; failures retain the work.
        if not self.erase_customer(uid, request):
            self.defer(uid, now)
            return False

        @firestore.transactional
        def finish(tx):
            current = doc.get(transaction=tx).to_dict()
            if current and current["state"] == "pending":
                tx.set(doc, {"state": "deleted", "completed_at": now})
        self.repo.transact(finish)
        return True

    def defer(self, uid, now):
        doc = deletion_document(self.db, uid)

        @firestore.transactional
        def write(tx):
            current = doc.get(transaction=tx).to_dict()
            if current and current["state"] == "pending":
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
