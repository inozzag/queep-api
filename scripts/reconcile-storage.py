"""One-account migration of legacy storage counters. No object is deleted.

Run with the matching environment, before enabling commercial billing for old accounts.
The inventory lock blocks concurrent mutations while exact Storage sizes are measured.
"""
import argparse
from datetime import UTC, datetime

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.account_state import require_open
from queep.accounts import Accounts
from queep.config import Settings, database, image_bucket
from queep.images import Images
from queep.repository import Repository
from queep.usage import inventory_document


def reconcile(uid):
    db, bucket = database(), image_bucket()
    repo, images = Repository(db), Images(db, bucket)
    stock = inventory_document(db, uid)
    now = datetime.now(UTC)

    @firestore.transactional
    def lock(tx):
        require_open(db, uid, tx)
        previous = stock.get(transaction=tx).to_dict()
        old = previous or {}
        if old.get("reconciling"):
            raise RuntimeError("Migration already locked; inspect before retrying")
        tx.set(stock, {**old, "reconciling": True, "locked_at": now})
        return previous
    previous = repo.transact(lock)
    # The bounded settle window must have elapsed; do not race existing upload writers.
    uploads = list(db.collection("image_uploads").where(
        filter=FieldFilter("uid", "==", uid)).stream())
    if any(u.to_dict().get("settle_at", now) > now for u in uploads):
        stock.set(previous) if previous is not None else stock.delete()
        raise RuntimeError("Uploads are still settling; retry after ten minutes")
    root = db.collection("users").document(uid)
    try:
        total = 0
        for document in uploads:
            upload = document.to_dict()
            size = 0
            for prefix in images.object_prefixes(repo.job(uid, upload["reference_id"]).id, upload):
                size += sum(blob.size or 0 for blob in bucket.list_blobs(prefix=prefix + "/"))
            document.reference.update({"storage_bytes": size})
            total += size
        account = Accounts(db, bucket, Settings())
        # Include failed/retired avatar uploads, which still cost storage.
        avatar_sizes = {}
        for blob in bucket.list_blobs(prefix=f"avatars/v1/{account.avatar_key(uid)}/"):
            prefix = blob.name.rsplit("/", 1)[0]
            account.validate_avatar_prefix(uid, prefix)
            avatar_sizes[prefix] = avatar_sizes.get(prefix, 0) + (blob.size or 0)
        root.set({"avatar_reservations": avatar_sizes,
                  "avatar_storage_bytes": sum(avatar_sizes.values())}, merge=True)
        total += sum(avatar_sizes.values())
        refs = [r.to_dict() for r in root.collection("references").stream()]
        stock.set({"items": sum(not r.get("deleted") or r.get("uploading", False) for r in refs),
                   "storage_bytes": total,
                   "spaces": sum(1 for _ in root.collection("spaces").stream()),
                   "reconciled_at": datetime.now(UTC), "reconciling": False})
        return {"storage_bytes": total, "completed": True}
    except Exception:
        # Leave the lock in place: a partial migration must not authorize underestimated usage.
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True)
    args = parser.parse_args()
    print(reconcile(args.uid))
