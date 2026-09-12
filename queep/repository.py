import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.account_state import require_open
from queep.models import Capture, Edit, Interpretation, Library, Reference
from queep.usage import reserve_operation


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def new_job(uid, ref_id, revision, now):
    return {"uid": uid, "reference_id": ref_id, "revision": revision, "state": "pending",
            "created_at": now, "dispatch_after": now, "run_id": uuid4().hex, "attempts": 0}


class Repository:
    def __init__(self, db):
        self.db = db

    def collection(self, uid):
        return self.db.collection("users").document(uid).collection("references")

    def job(self, uid, ref_id):
        key = hashlib.sha256(f"{uid}/{ref_id}".encode()).hexdigest()
        return self.db.collection("jobs").document(key)

    def transact(self, operation):
        try:
            return operation(self.db.transaction())
        except (Aborted, DeadlineExceeded, ServiceUnavailable) as error:
            raise HTTPException(503, "Serviço ocupado. Tente novamente.",
                                headers={"Retry-After": "1"}) from error
        except ValueError as error:
            # The Firestore SDK wraps exhausted contention retries in ValueError.
            if not isinstance(error.__cause__, Aborted):
                raise
            raise HTTPException(503, "Serviço ocupado. Tente novamente.",
                                headers={"Retry-After": "1"}) from error

    def save(self, uid: str, ref_id: str, capture: Capture, editing=False):
        doc = self.collection(uid).document(ref_id)
        job = self.job(uid, ref_id)
        payload = capture.model_dump(mode="json", exclude={"revision", "mutation_id"})
        digest = fingerprint(payload)
        now = datetime.now(UTC)

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if editing:
                if not old or old.get("deleted"):
                    raise HTTPException(404, "Referência não encontrada")
                if old["kind"] != capture.kind:
                    raise HTTPException(409, "O tipo da referência não pode ser alterado")
                assert isinstance(capture, Edit)
                mutation_id = str(capture.mutation_id)
                if old.get("last_mutation_id") == mutation_id:
                    if old["content_hash"] != digest:
                        raise HTTPException(409, "Identificador de edição reutilizado")
                    return old
                if old["revision"] != capture.revision:
                    raise HTTPException(409, "A referência mudou. Atualize antes de editar")
            elif old:
                if old.get("deleted") or old["create_hash"] != digest:
                    raise HTTPException(409, "Identificador de envio já utilizado")
                return old
            reserve_operation(self.db, uid, tx, now=now)
            record = {
                **payload, "id": ref_id, "status": "queued", "deleted": False,
                "revision": old["revision"] + 1 if old else 1,
                "created_at": old["created_at"] if old else now, "updated_at": now,
                "analysis": None, "content_hash": digest,
                "create_hash": old["create_hash"] if old else digest,
                "last_mutation_id": str(capture.mutation_id) if editing else None,
            }
            tx.set(doc, record)
            # Outbox and content are atomic. A later dispatcher can safely enqueue Cloud Tasks.
            tx.set(job, new_job(uid, ref_id, record["revision"], now))
            return record

        return Reference.model_validate(self.transact(write))

    def get(self, uid, ref_id):
        require_open(self.db, uid)
        record = self.collection(uid).document(ref_id).get().to_dict()
        if not record or record.get("deleted"):
            raise HTTPException(404, "Referência não encontrada")
        return Reference.model_validate(record)

    def list(self, uid, limit, cursor):
        require_open(self.db, uid)
        query = (self.collection(uid).where(filter=FieldFilter("deleted", "==", False))
                 .order_by("created_at", direction=firestore.Query.DESCENDING)
                 .order_by("id", direction=firestore.Query.DESCENDING))
        if cursor:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                stamp = datetime.fromisoformat(raw["created_at"])
                if stamp.tzinfo is None or not isinstance(raw["id"], str):
                    raise ValueError("Invalid cursor")
                query = query.start_after({"created_at": stamp, "id": raw["id"]})
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(400, "Cursor inválido") from exc
        records = list(query.limit(limit + 1).stream())
        page = [Reference.model_validate(doc.to_dict()) for doc in records[:limit]]
        next_cursor = None
        if len(records) > limit:
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "id": page[-1].id, "created_at": page[-1].created_at.isoformat(),
            }).encode()).decode()
        return Library(items=page, next_cursor=next_cursor)

    def delete(self, uid, ref_id, revision):
        doc = self.collection(uid).document(ref_id)
        job = self.job(uid, ref_id)
        upload = self.db.collection("image_uploads").document(job.id)

        @firestore.transactional
        def remove(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if not old or old.get("deleted"):
                return
            if old["revision"] != revision:
                raise HTTPException(409, "A referência mudou. Atualize antes de excluir")
            # Keep only a tombstone: late retries must not recreate deleted private content.
            tx.set(doc, {"deleted": True, "revision": revision + 1,
                         "updated_at": datetime.now(UTC)})
            tx.delete(job)
            if old.get("image") or old.get("media"):
                # Persist cleanup with the tombstone; Storage failures cannot lose the work.
                from datetime import timedelta
                tx.update(upload, {"state": "cleanup", "cleanup_after": datetime.now(UTC),
                                   "settle_at": datetime.now(UTC) + timedelta(minutes=10)})

        self.transact(remove)

    def complete(self, uid, ref_id, revision, analysis: Interpretation):
        """Publish only if this job still belongs to the current, undeleted revision."""
        doc = self.collection(uid).document(ref_id)
        job = self.job(uid, ref_id)

        @firestore.transactional
        def publish(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if not old or old.get("deleted") or old["revision"] != revision:
                return False
            tx.update(doc, {"status": "ready", "analysis": analysis.model_dump(),
                            "updated_at": datetime.now(UTC)})
            tx.delete(job)
            return True

        return self.transact(publish)
