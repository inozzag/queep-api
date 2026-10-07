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
from queep.availability import retry_later
from queep.config import Settings
from queep.models import Capture, Edit, Interpretation, Library, MetadataEdit, Reference
from queep.usage import inventory, policy, reserve_operation


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def new_job(uid, ref_id, revision, now):
    return {"uid": uid, "reference_id": ref_id, "revision": revision, "state": "pending",
            "created_at": now, "dispatch_after": now, "run_id": uuid4().hex, "attempts": 0}


class Repository:
    def __init__(self, db):
        self.db = db
        self.job_collection = Settings().job_collection

    def collection(self, uid):
        return self.db.collection("users").document(uid).collection("references")

    def jobs(self):
        return self.db.collection(self.job_collection)

    def job(self, uid, ref_id):
        key = hashlib.sha256(f"{uid}/{ref_id}".encode()).hexdigest()
        return self.jobs().document(key)

    def transact(self, operation):
        try:
            return operation(self.db.transaction())
        except (Aborted, DeadlineExceeded, ServiceUnavailable) as error:
            raise retry_later() from error
        except ValueError as error:
            # The Firestore SDK wraps exhausted contention retries in ValueError.
            if not isinstance(error.__cause__, Aborted):
                raise
            raise retry_later() from error

    def save(self, uid: str, ref_id: str, capture: Capture, editing=False):
        access = policy(self.db, uid)
        doc = self.collection(uid).document(ref_id)
        job = self.job(uid, ref_id)
        payload = capture.model_dump(mode="json", exclude={"revision", "mutation_id"})
        space_ids = sorted(set(payload.pop("space_ids", [])))
        if editing and space_ids:
            raise HTTPException(422, "Use a ação Space para alterar os vínculos")
        # Keep fingerprints compatible with captures made before Spaces existed.
        if space_ids:
            payload["space_ids"] = space_ids
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
            if not editing and space_ids:
                from queep.spaces import Spaces
                Spaces(self.db).require(uid, space_ids, tx)
            reserve_operation(self.db, uid, tx, now=now, access=access, items=int(not old))
            record = {
                **payload, "id": ref_id, "status": "queued", "deleted": False,
                "revision": old["revision"] + 1 if old else 1,
                "created_at": old["created_at"] if old else now, "updated_at": now,
                "archived_at": old.get("archived_at") if old else None,
                "space_ids": old.get("space_ids", []) if old else space_ids,
                "analysis": None, "content_hash": digest,
                "create_hash": old["create_hash"] if old else digest,
                "last_mutation_id": str(capture.mutation_id) if editing else None,
            }
            tx.set(doc, record)
            # Outbox and content are atomic. A later dispatcher can safely enqueue Cloud Tasks.
            tx.set(job, new_job(uid, ref_id, record["revision"], now) | {"plan": access.plan})
            return record

        return Reference.model_validate(self.transact(write))

    def edit_metadata(self, uid: str, ref_id: str, edit: MetadataEdit):
        """Title/notes of an upload, or the title of a social post. Uploads requeue analysis."""
        access = policy(self.db, uid)
        doc = self.collection(uid).document(ref_id)
        job = self.job(uid, ref_id)
        now = datetime.now(UTC)

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if not old or old.get("deleted"):
                raise HTTPException(404, "Referência não encontrada")
            if old["kind"] != edit.kind:
                raise HTTPException(409, "O tipo da referência não pode ser alterado")
            digest = fingerprint({"kind": edit.kind, "title": edit.title, "text": edit.text,
                                  "file": old["create_hash"]})
            mutation_id = str(edit.mutation_id)
            if old.get("last_mutation_id") == mutation_id:
                if old["content_hash"] != digest:
                    raise HTTPException(409, "Identificador de edição reutilizado")
                return old
            if old["revision"] != edit.revision:
                raise HTTPException(409, "A referência mudou. Atualize antes de editar")
            changes = {"title": edit.title, "revision": old["revision"] + 1,
                       "updated_at": now, "content_hash": digest,
                       "last_mutation_id": mutation_id}
            if edit.kind == "link":
                tx.update(doc, changes)
                return {**old, **changes}
            reserve_operation(self.db, uid, tx, now=now, access=access,
                              media_seconds=(old.get("media") or {}).get("duration_seconds", 0))
            changes.update({"text": edit.text, "status": "queued", "analysis": None})
            tx.update(doc, changes)
            tx.set(job, new_job(uid, ref_id, changes["revision"], now) | {"plan": access.plan})
            return {**old, **changes}

        return Reference.model_validate(self.transact(write))

    def get(self, uid, ref_id):
        require_open(self.db, uid)
        record = self.collection(uid).document(ref_id).get().to_dict()
        if not record or record.get("deleted"):
            raise HTTPException(404, "Referência não encontrada")
        return Reference.model_validate(record)

    def list(self, uid, limit, cursor, archived=False, space_id=None):
        require_open(self.db, uid)
        if archived:
            query = (self.collection(uid).where(filter=FieldFilter("deleted", "==", False))
                     .where(filter=FieldFilter(
                         "archived_at", ">=", datetime(1970, 1, 1, tzinfo=UTC)))
                     .order_by("archived_at", direction=firestore.Query.DESCENDING)
                     .order_by("id", direction=firestore.Query.DESCENDING))
            cursor_field = "archived_at"
        else:
            # Legacy documents do not have archived_at. Keep the existing indexed query and
            # filter timestamps in bounded chunks so a backfill is not required.
            query = (self.collection(uid).where(filter=FieldFilter("deleted", "==", False))
                     .order_by("created_at", direction=firestore.Query.DESCENDING)
                     .order_by("id", direction=firestore.Query.DESCENDING))
            cursor_field = "created_at"
        if space_id:
            from queep.spaces import Spaces
            Spaces(self.db).require(uid, [space_id])
            query = query.where(filter=FieldFilter("space_ids", "array_contains", space_id))
        if cursor:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if raw.get("space_id") != space_id:
                    raise ValueError("Cursor belongs to another Space")
                if raw.get("archived") is not archived:
                    raise ValueError("Cursor belongs to another archive view")
                stamp = datetime.fromisoformat(raw[cursor_field])
                if stamp.tzinfo is None or not isinstance(raw["id"], str):
                    raise ValueError("Invalid cursor")
                query = query.start_after({cursor_field: stamp, "id": raw["id"]})
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(400, "Cursor inválido") from exc

        if archived:
            records = list(query.limit(limit + 1).stream())
            visible = [Reference.model_validate(doc.to_dict()) for doc in records]
        else:
            visible = []
            scan = query
            chunk_size = max(100, limit + 1)
            while len(visible) < limit + 1:
                records = list(scan.limit(chunk_size).stream())
                if not records:
                    break
                for document in records:
                    record = document.to_dict()
                    if record.get("archived_at") is None:
                        visible.append(Reference.model_validate(record))
                        if len(visible) == limit + 1:
                            break
                if len(records) < chunk_size or len(visible) == limit + 1:
                    break
                last = records[-1].to_dict()
                scan = query.start_after({"created_at": last["created_at"], "id": last["id"]})

        page = visible[:limit]
        next_cursor = None
        if len(visible) > limit:
            stamp = getattr(page[-1], cursor_field)
            next_cursor = base64.urlsafe_b64encode(json.dumps({
                "id": page[-1].id, cursor_field: stamp.isoformat(), "archived": archived,
                "space_id": space_id,
            }).encode()).decode()
        return Library(items=page, next_cursor=next_cursor)

    def archive(self, uid, ref_id, archived):
        doc = self.collection(uid).document(ref_id)
        now = datetime.now(UTC)

        @firestore.transactional
        def update(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if not old or old.get("deleted"):
                raise HTTPException(404, "Referência não encontrada")
            if (old.get("archived_at") is not None) == archived:
                return old
            changes = {"archived_at": now if archived else None, "updated_at": now}
            tx.update(doc, changes)
            return {**old, **changes}

        return Reference.model_validate(self.transact(update))

    def batch_archive(self, uid, ref_ids, archived):
        # Keep caller order stable and make retries with duplicate IDs idempotent.
        unique_ids = list(dict.fromkeys(ref_ids))
        documents = [self.collection(uid).document(ref_id) for ref_id in unique_ids]
        now = datetime.now(UTC)

        @firestore.transactional
        def update(tx):
            require_open(self.db, uid, tx)
            records = [document.get(transaction=tx).to_dict() for document in documents]
            for record in records:
                if not record or record.get("deleted"):
                    raise HTTPException(404, "Referência não encontrada")
            output = []
            for document, old in zip(documents, records, strict=True):
                if (old.get("archived_at") is not None) == archived:
                    output.append(old)
                    continue
                changes = {"archived_at": now if archived else None, "updated_at": now}
                tx.update(document, changes)
                output.append({**old, **changes})
            return output

        return [Reference.model_validate(record) for record in self.transact(update)]

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
            assets = upload.get(transaction=tx).to_dict()
            stock, stored = inventory(self.db, uid, tx)
            tx.set(stock, {**stored, "items": max(0, stored.get("items", 0) - 1)})
            # Keep only a tombstone: late retries must not recreate deleted private content.
            tx.set(doc, {"deleted": True, "revision": revision + 1,
                         "updated_at": datetime.now(UTC)})
            tx.delete(job)
            if assets:
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
