"""Re-read public favicons, pictures and video posters already saved on an account.

Does not create new references and does not call the model again. A link is left
untouched when the new read returns no picture and no icon.
"""

import os
import sys
import time
from datetime import UTC, datetime

HOSTS = ("motionsites", "fontshare", "indiehackers", "instagram.com", "news.ycombinator")
WEAK_TITLES = {"", "instagram", "post", "link", "site"}


def configure():
    os.environ.setdefault("QUEEP_ENV", "production")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "queep-dev-hstlx")
    os.environ.setdefault("QUEEP_STORAGE_BUCKET", "queep-dev-hstlx.firebasestorage.app")
    # Same mode as the development API the phone uses. Required mode calls RevenueCat,
    # and that secret is not on this machine, so every picture would be skipped.
    os.environ.setdefault("QUEEP_BILLING_MODE", "development")


def accounts(db):
    found = []
    for doc in db.collection("users").stream():
        data = doc.to_dict() or {}
        access = data.get("access") or {}
        found.append((doc.id, access.get("plan") or "unknown"))
    return found


def owns_library(db, uid):
    for doc in db.collection("users").document(uid).collection("references").limit(80).stream():
        record = doc.to_dict() or {}
        blob = " ".join(str(record.get(key) or "") for key in ("url", "title"))
        if any(host in blob for host in HOSTS):
            return True
    return False


def refresh_one(db, images, uid, ref_id, record):
    from queep.models import Reference
    from queep.social import is_profile
    from queep.sources import SourceError, checked_url, read_link
    from queep.worker import apply_previews

    if record.get("deleted") or record.get("kind") != "link" or not record.get("url"):
        return "skip"
    source, bundle = None, None
    for attempt in range(2):
        try:
            source, bundle = read_link(record["url"])
        except (SourceError, OSError, ValueError):
            source, bundle = None, None
        if source is not None and (source.state == "read" or bundle):
            break
        if attempt == 0:
            time.sleep(0.8)
    if source is None:
        return "unread"
    if isinstance(source, tuple):
        source, bundle = source
    try:
        ref = Reference.model_validate(record)
    except (TypeError, ValueError):
        return "invalid"
    try:
        profile = bool(is_profile(checked_url(record["url"])))
    except SourceError:
        profile = False
    page = bool(source and source.content_kind == "page")
    updated, _ = apply_previews(uid, ref, bundle, images, None, page, profile)
    payload = {}
    if updated.images:
        fresh = [item.model_dump() for item in updated.images]
        old_images = list(record.get("images") or [])
        old_shas = {item.get("sha256") for item in old_images}
        fresh_shas = [item.get("sha256") for item in fresh]
        # A full gallery makes attach_preview hand back an old item. Never shrink that set.
        already_stored = bool(fresh_shas) and all(sha in old_shas for sha in fresh_shas)
        if not (already_stored and len(fresh) < len(old_images)):
            payload["images"] = fresh
            payload["image"] = fresh[0]
    if updated.author_image is not None:
        payload["author_image"] = updated.author_image.model_dump()
    old = record.get("source") or {}
    old_title = (old.get("title") or "").strip().lower()
    fresh_title = source.title.strip().lower() if source is not None else ""
    if source is not None and source.state == "read" and fresh_title not in WEAK_TITLES:
        if old_title in WEAK_TITLES or not old.get("text"):
            payload["source"] = source.model_dump(mode="json")
    if not payload:
        return "empty"
    payload["updated_at"] = datetime.now(UTC)
    db.collection("users").document(uid).collection("references").document(ref_id).update(payload)
    kinds = ",".join(item.preview_kind or "photo" for item in (updated.images or []))
    host = record["url"].split("/")[2] if "://" in record["url"] else record["url"]
    print(f"  updated {host} images={kinds or '-'} icon={'yes' if updated.author_image else 'no'}")
    return "updated"


def main():
    configure()
    from queep.config import database, image_bucket
    from queep.images import Images

    db = database()
    listed = accounts(db)
    if not listed:
        print("no accounts")
        return 1
    for uid, plan in listed:
        print(f"account {uid} plan={plan}")
    chosen = [uid for uid, plan in listed if plan == "max"]
    if not chosen:
        chosen = [uid for uid, _ in listed if owns_library(db, uid)]
    if len(chosen) != 1:
        print(f"expected one max library, found {len(chosen)}")
        return 1
    uid = chosen[0]
    images = Images(db, image_bucket())
    counts = {}
    docs = list(db.collection("users").document(uid).collection("references").stream())
    print(f"refreshing {len(docs)} references")
    for doc in docs:
        status = refresh_one(db, images, uid, doc.id, doc.to_dict() or {})
        counts[status] = counts.get(status, 0) + 1
        time.sleep(0.2)
    print(counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
