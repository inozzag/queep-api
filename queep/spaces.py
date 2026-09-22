"""Personal, non-exclusive collections. Membership never changes content or AI usage."""

from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.account_state import require_open
from queep.models import Reference, Space
from queep.plans import LIMITS, denied
from queep.usage import inventory, policy


class Spaces:
    def __init__(self, db):
        # Local import keeps the repository's capture validation free of import cycles.
        from queep.repository import Repository
        self.db = db
        self.repo = Repository(db)

    def collection(self, uid):
        return self.db.collection("users").document(uid).collection("spaces")

    def require(self, uid, identifiers, tx=None):
        for identifier in set(map(str, identifiers)):
            record = self.collection(uid).document(identifier).get(transaction=tx).to_dict()
            if not record or record.get("deleting"):
                raise HTTPException(404, "Space não encontrado")

    def counts(self, uid):
        counts = Counter()
        for doc in self.repo.collection(uid).where(
            filter=FieldFilter("deleted", "==", False)
        ).select(["space_ids", "archived_at"]).stream():
            row = doc.to_dict()
            if not row.get("archived_at"):
                counts.update(set(row.get("space_ids", [])))
        return counts

    def list(self, uid):
        require_open(self.db, uid)
        rows = [doc.to_dict() for doc in self.collection(uid).stream()
                if not doc.to_dict().get("deleting")]
        counts = self.counts(uid) if rows else {}
        return [Space.model_validate(row | {"reference_count": counts.get(row["id"], 0)})
                for row in sorted(rows, key=lambda row: (row["sort_index"], row["id"]))]

    def save(self, uid, change, identifier=None):
        access = policy(self.db, uid) if identifier is None else None
        creating = identifier is None
        identifier = identifier or str(uuid4())
        now = datetime.now(UTC)

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            # A shared inventory document serializes create/rename/delete, including empty
            # query results, so both name uniqueness and the plan cap survive concurrency.
            stock, stored = inventory(self.db, uid, tx)
            rows = [doc.to_dict() for doc in self.collection(uid).stream(transaction=tx)
                    if not doc.to_dict().get("deleting")]
            rows.sort(key=lambda row: (row["sort_index"], row["id"]))
            old = next((row for row in rows if row["id"] == identifier), None)
            if not creating and not old:
                raise HTTPException(404, "Space não encontrado")
            name = change.name if change.name is not None else old["name"]
            name_key = name.casefold()
            if any(row["name_key"] == name_key and row["id"] != identifier for row in rows):
                raise HTTPException(409, "Já existe um Space com este nome")
            if creating and len(rows) >= LIMITS[access.plan]["spaces"]:
                raise denied(access.plan, "spaces")
            record = {"id": identifier, "name": name, "name_key": name_key,
                      "sort_index": old["sort_index"] if old else len(rows),
                      "created_at": old["created_at"] if old else now, "updated_at": now}
            rows = [row for row in rows if row["id"] != identifier]
            index = getattr(change, "sort_index", None)
            rows.insert(min(index if index is not None else record["sort_index"], len(rows)),
                        record)
            for index, row in enumerate(rows):
                if row["id"] == identifier or row["sort_index"] != index:
                    row.update(sort_index=index, updated_at=now)
                    tx.set(self.collection(uid).document(row["id"]), row)
            tx.set(stock, {**stored, "spaces": len(rows)})
            return record

        record = self.repo.transact(write)
        return Space.model_validate(record | {"reference_count": self.counts(uid)[identifier]})

    def assign(self, uid, ref_id, change):
        add, remove = set(map(str, change.add)), set(map(str, change.remove))
        doc = self.repo.collection(uid).document(ref_id)

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if not old or old.get("deleted"):
                raise HTTPException(404, "Referência não encontrada")
            self.require(uid, add | remove, tx)
            ids = sorted((set(old.get("space_ids", [])) | add) - remove)
            if ids != old.get("space_ids", []):
                tx.update(doc, {"space_ids": ids})
            return old | {"space_ids": ids}

        return Reference.model_validate(self.repo.transact(write))

    def delete(self, uid, identifier):
        doc = self.collection(uid).document(identifier)
        cleanup = self.db.collection("space_cleanup").document(self.repo.job(uid, identifier).id)

        @firestore.transactional
        def mark(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict()
            if not old or old.get("deleting"):
                return
            stock, stored = inventory(self.db, uid, tx)
            tx.update(doc, {"deleting": True})
            tx.set(stock, {**stored, "spaces": max(0, stored.get("spaces", 0) - 1)})
            tx.set(cleanup, {"uid": uid, "space_id": identifier})

        self.repo.transact(mark)

    def cleanup(self, uid, identifier):
        """Bounded, retryable batches; deleting Space prevents any new assignments."""
        doc = self.collection(uid).document(identifier)
        if not (doc.get().to_dict() or {}).get("deleting"):
            return
        query = self.repo.collection(uid).where(
            filter=FieldFilter("space_ids", "array_contains", identifier))
        while True:
            records = list(query.limit(400).stream())
            if not records:
                break

            @firestore.transactional
            def remove(tx, records=records):
                # Read again to avoid recreating deleted references or clobbering a
                # concurrent assignment to another Space.
                current = list(self.db.get_all([r.reference for r in records], transaction=tx))
                for item in current:
                    if item.exists and identifier in item.to_dict().get("space_ids", []):
                        tx.update(item.reference, {
                            "space_ids": firestore.ArrayRemove([identifier])})

            self.repo.transact(remove)
        batch = self.db.batch()
        batch.delete(doc)
        cleanup_id = self.repo.job(uid, identifier).id
        batch.delete(self.db.collection("space_cleanup").document(cleanup_id))
        batch.commit()

    def sweep(self):
        count = 0
        for doc in self.db.collection("space_cleanup").limit(50).stream():
            row = doc.to_dict()
            self.cleanup(row["uid"], row["space_id"])
            count += 1
        return count
