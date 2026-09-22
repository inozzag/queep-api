"""Monthly commercial quotas and separate daily abuse limits, reserved atomically."""

import math
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.account_state import require_open
from queep.config import Settings
from queep.plans import denied, month_window


def policy(db, uid):
    from queep.billing import Billing
    return Billing(db, Settings()).access(uid)


def window(now=None):
    now = now or datetime.now(UTC)
    reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    return now, reset


def document(db, uid, now):
    return db.collection("users").document(uid).collection("usage").document(now.strftime("%Y-%m"))


def daily_document(db, uid, now):
    return (db.collection("users").document(uid).collection("fair_use")
            .document(now.date().isoformat()))


def inventory_document(db, uid):
    return db.collection("users").document(uid).collection("inventory").document("current")


def inventory(db, uid, tx=None):
    doc = inventory_document(db, uid)
    old = doc.get(transaction=tx).to_dict()
    if old is not None:
        if old.get("reconciling"):
            raise HTTPException(503, "Armazenamento em atualização. Tente novamente.")
        return doc, old
    root = db.collection("users").document(uid)
    refs = list(root.collection("references").stream(transaction=tx))
    uploads = list(db.collection("image_uploads").where(
        filter=FieldFilter("uid", "==", uid)).stream(transaction=tx))
    # Historical manifests do not include thumbnails' exact size. Never undercount them.
    if any("storage_bytes" not in upload.to_dict() for upload in uploads):
        raise HTTPException(503, "Armazenamento em atualização. Tente novamente.")
    profile = root.get(transaction=tx).to_dict() or {}
    if profile.get("avatar_prefix") and "avatar_storage_bytes" not in profile:
        raise HTTPException(503, "Armazenamento em atualização. Tente novamente.")
    return doc, {
        "items": sum(not r.to_dict().get("deleted") or r.to_dict().get("uploading", False)
                     for r in refs),
        "storage_bytes": sum(u.to_dict()["storage_bytes"] for u in uploads)
                         + profile.get("avatar_storage_bytes", 0),
        "spaces": sum(not doc.to_dict().get("deleting")
                      for doc in root.collection("spaces").stream(transaction=tx)),
    }


def limits(settings):
    return {
        "operations": settings.operation_daily_limit,
        "upload_bytes": settings.upload_daily_mib * 1024 * 1024,
        "media_seconds": settings.media_daily_minutes * 60,
        "searches": settings.search_daily_limit,
    }


def exceeded(message, reset, now):
    return HTTPException(
        429, message, headers={"Retry-After": str(max(1, math.ceil((reset - now).total_seconds())))}
    )


def check_inventory(access, old, *, items=0, storage_bytes=0, now=None):
    for key, amount in (("items", items), ("storage_bytes", storage_bytes)):
        if old.get(key, 0) + amount > access.limits[key]:
            raise denied(access.plan, key, now=now)


def reserve_operation(db, uid, tx, *, access, upload_bytes=0, media_seconds=0,
                      items=0, now=None):
    """All reads precede writes; exact retries must return before this reservation."""
    now, reset = month_window(now)
    doc = document(db, uid, now)
    daily = daily_document(db, uid, now)
    old = doc.get(transaction=tx).to_dict() or {}
    abuse = daily.get(transaction=tx).to_dict() or {}
    stock, stored = inventory(db, uid, tx)
    if media_seconds and not access.features["media_av"]:
        raise denied(access.plan, "media_av", now=now)
    check_inventory(access, stored, items=items, storage_bytes=upload_bytes, now=now)
    changes = {"operations": 1, "upload_bytes": upload_bytes,
               "media_seconds": math.ceil(media_seconds)}
    fair_use = limits(Settings())
    for key, amount in changes.items():
        if old.get(key, 0) + amount > access.limits[key]:
            raise denied(access.plan, key, now=now)
        if abuse.get(key, 0) + amount > fair_use[key]:
            raise exceeded("Limite diário de proteção atingido. Tente novamente amanhã.",
                           window(now)[1], now)
    tx.set(doc, {key: old.get(key, 0) + value for key, value in changes.items()}
           | {"expires_at": reset + timedelta(days=90)}, merge=True)
    tx.set(daily, {key: abuse.get(key, 0) + value for key, value in changes.items()}
           | {"expires_at": window(now)[1] + timedelta(days=30)}, merge=True)
    tx.set(stock, {**stored, "items": stored.get("items", 0) + items,
                   "storage_bytes": stored.get("storage_bytes", 0) + upload_bytes})


def snapshot(db, uid, settings, now=None, access=None):
    require_open(db, uid)
    access = access or policy(db, uid)
    now, reset = month_window(now)
    old = document(db, uid, now).get().to_dict() or {}
    _, stored = inventory(db, uid)
    return {
        "plan": access.plan,
        "resets_at": reset,
        "counters": {key: {"used": (stored if key in ("items", "storage_bytes", "spaces")
                                    else old).get(key, 0), "limit": value}
                     for key, value in access.limits.items()},
    }


def reserve_storage(db, uid, tx, access, size, *, upload=False):
    """Reserve profile assets without charging an AI operation."""
    now, reset = month_window()
    stock, stored = inventory(db, uid, tx)
    check_inventory(access, stored, storage_bytes=size)
    monthly = document(db, uid, now)
    daily = daily_document(db, uid, now)
    if upload:
        old = monthly.get(transaction=tx).to_dict() or {}
        abuse = daily.get(transaction=tx).to_dict() or {}
        if old.get("upload_bytes", 0) + size > access.limits["upload_bytes"]:
            raise denied(access.plan, "upload_bytes", now=now)
        if abuse.get("upload_bytes", 0) + size > limits(Settings())["upload_bytes"]:
            raise exceeded("Limite diário de arquivos atingido.", window(now)[1], now)
        tx.set(monthly, {"upload_bytes": old.get("upload_bytes", 0) + size,
                         "expires_at": reset + timedelta(days=90)}, merge=True)
        tx.set(daily, {"upload_bytes": abuse.get("upload_bytes", 0) + size,
                       "expires_at": window(now)[1] + timedelta(days=30)}, merge=True)
    tx.set(stock, {**stored, "storage_bytes": stored.get("storage_bytes", 0) + size})
