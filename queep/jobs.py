"""Transactional outbox, leases and revision-safe publication of AI work."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.vector import Vector

from queep.account_state import account_version, is_closed, require_open
from queep.ai import ANALYSIS_VERSION, INTERPRETER, SPACE
from queep.models import Reference
from queep.repository import Repository, new_job


class Jobs:
    MAX_ATTEMPTS = 3
    LEASE_SECONDS = 180

    def __init__(self, db):
        self.db, self.repo = db, Repository(db)

    def bootstrap(self, job_id):
        """Upgrade the outbox left by the capture-only release without losing newer work."""
        doc = self.db.collection("jobs").document(job_id)

        @firestore.transactional
        def write(tx):
            old = doc.get(transaction=tx).to_dict()
            if old and "run_id" not in old:
                if is_closed(self.db, old["uid"], tx):
                    return
                tx.set(doc, new_job(old["uid"], old["reference_id"], old["revision"],
                                    datetime.now(UTC)))
        self.repo.transact(write)

    def due(self):
        collection = self.db.collection("jobs")
        pending = collection.where(filter=FieldFilter("state", "==", "pending"))
        for doc in pending.limit(100).stream():
            if "run_id" not in doc.to_dict():
                self.bootstrap(doc.id)
        candidates = list(collection.where(
            filter=FieldFilter("dispatch_after", "<=", datetime.now(UTC)))
            .order_by("dispatch_after").limit(50).stream())
        ready = []
        for doc in candidates:
            if is_closed(self.db, doc.to_dict()["uid"]):
                self.pause(doc.id)
            else:
                ready.append(doc)
        return ready

    def pause(self, job_id):
        doc = self.db.collection("jobs").document(job_id)

        @firestore.transactional
        def write(tx):
            job = doc.get(transaction=tx).to_dict()
            if job and is_closed(self.db, job["uid"], tx):
                tx.update(doc, {"state": "pending", "run_id": uuid4().hex,
                                "attempts": max(0, job["attempts"] -
                                                int(job["state"] == "processing")),
                                "token": firestore.DELETE_FIELD,
                                "lease_until": firestore.DELETE_FIELD,
                                "dispatch_after": datetime.now(UTC) + timedelta(minutes=5)})
        self.repo.transact(write)

    def payload(self, doc):
        job = doc.to_dict()
        return {"job_id": doc.id, "run_id": job["run_id"], "revision": job["revision"],
                "attempt": job["attempts"]}

    def matches(self, job, task):
        return (job and job.get("run_id") == task["run_id"]
                and job["revision"] == task["revision"])

    def enqueued(self, task):
        doc = self.db.collection("jobs").document(task["job_id"])

        @firestore.transactional
        def write(tx):
            job = doc.get(transaction=tx).to_dict()
            if (self.matches(job, task) and job["attempts"] == task["attempt"]
                    and job["state"] == "pending"):
                tx.update(doc, {"dispatch_after": datetime.now(UTC) + timedelta(minutes=5)})
        self.repo.transact(write)

    def claim(self, task, now=None):
        now = now or datetime.now(UTC)
        doc = self.db.collection("jobs").document(task["job_id"])
        token = uuid4().hex

        @firestore.transactional
        def write(tx):
            job = doc.get(transaction=tx).to_dict()
            if not self.matches(job, task) or job["state"] == "failed":
                return None
            if is_closed(self.db, job["uid"], tx):
                return None
            version = account_version(self.db, job["uid"], tx)
            expected_attempt = task["attempt"]
            if job["state"] == "processing" and job.get("account_version", 0) != version:
                if expected_attempt == job["attempts"]:
                    expected_attempt = max(0, expected_attempt - 1)
                job = {**job, "attempts": max(0, job["attempts"] - 1)}
                job.pop("lease_until", None)
            reference = self.repo.collection(job["uid"]).document(job["reference_id"])
            record = reference.get(transaction=tx).to_dict()
            if not record or record.get("deleted") or record["revision"] != task["revision"]:
                tx.delete(doc)
                return None
            if job["attempts"] >= self.MAX_ATTEMPTS:
                if job.get("lease_until", now) > now:
                    return None
                tx.update(doc, {"state": "failed", "dispatch_after": firestore.DELETE_FIELD})
                tx.update(reference, {"status": "error", "updated_at": now})
                return None
            if job["attempts"] != expected_attempt or job.get("lease_until", now) > now:
                return None
            lease = now + timedelta(seconds=self.LEASE_SECONDS)
            tx.update(doc, {"state": "processing", "account_version": version,
                            "token": token, "lease_until": lease,
                            "dispatch_after": lease, "attempts": job["attempts"] + 1})
            tx.update(reference, {"status": "processing", "updated_at": now})
            return {**job, "job_id": doc.id, "account_version": version,
                    "token": token, "attempts": job["attempts"] + 1,
                    "reference": Reference.model_validate(record)}
        return self.repo.transact(write)

    def current(self, work):
        if (is_closed(self.db, work["uid"])
                or account_version(self.db, work["uid"]) != work.get("account_version", 0)):
            return False
        job = self.db.collection("jobs").document(work["job_id"]).get().to_dict()
        return bool(self.matches(job, work) and job.get("token") == work["token"]
                    and job["state"] == "processing" and job["lease_until"] > datetime.now(UTC))

    def finish(self, work, analysis=None, vector=None, error=None, source=None):
        doc = self.db.collection("jobs").document(work["job_id"])
        ref = self.repo.collection(work["uid"]).document(work["reference_id"])
        now = datetime.now(UTC)

        @firestore.transactional
        def write(tx):
            if (is_closed(self.db, work["uid"], tx)
                    or account_version(self.db, work["uid"], tx) != work.get("account_version", 0)):
                return False
            job = doc.get(transaction=tx).to_dict()
            record = ref.get(transaction=tx).to_dict()
            if (not self.matches(job, work) or job.get("token") != work["token"]
                    or job["state"] != "processing" or job["lease_until"] <= now
                    or not record or record.get("deleted")
                    or record["revision"] != work["revision"]):
                return False
            if error:
                failed = job["attempts"] >= self.MAX_ATTEMPTS
                tx.update(ref, {"status": "error" if failed else "queued", "updated_at": now})
                tx.update(doc, {"state": "failed" if failed else "pending", "error": error,
                                "token": firestore.DELETE_FIELD,
                                "lease_until": firestore.DELETE_FIELD,
                                "dispatch_after": firestore.DELETE_FIELD if failed
                                else now + timedelta(seconds=30 * job["attempts"])})
            else:
                tx.update(ref, {"status": "ready", "analysis": analysis.model_dump(),
                                "source": source.model_dump() if source else None,
                                "embedding": Vector(vector), "embedding_space": SPACE,
                                "analysis_model": INTERPRETER["id"],
                                "analysis_version": ANALYSIS_VERSION, "updated_at": now})
                tx.delete(doc)
            return True
        return self.repo.transact(write)

    def retry(self, uid, ref_id, revision):
        doc = self.repo.collection(uid).document(ref_id)
        job = self.repo.job(uid, ref_id)

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if not old or old.get("deleted"):
                raise HTTPException(404, "Referência não encontrada")
            if old["revision"] != revision:
                raise HTTPException(409, "A referência mudou. Atualize antes de tentar")
            if old["status"] != "error":
                return Reference.model_validate(old)
            now = datetime.now(UTC)
            tx.update(doc, {"status": "queued", "updated_at": now})
            tx.set(job, new_job(uid, ref_id, revision, now))
            return Reference.model_validate({**old, "status": "queued", "updated_at": now})
        return self.repo.transact(write)
