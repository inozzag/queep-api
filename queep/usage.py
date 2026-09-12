"""Daily operational limits, reserved atomically with the work they authorize."""

import math
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from queep.account_state import require_open
from queep.config import Settings


def window(now=None):
    now = now or datetime.now(UTC)
    reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    return now, reset


def document(db, uid, now):
    return db.collection("users").document(uid).collection("usage").document(now.date().isoformat())


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


def reserve_operation(db, uid, tx, *, upload_bytes=0, media_seconds=0, now=None):
    """Call after other reads, before writes; caller has checked account/idempotency."""
    now, reset = window(now)
    doc = document(db, uid, now)
    old = doc.get(transaction=tx).to_dict() or {}
    caps = limits(Settings())
    changes = {
        "operations": 1,
        "upload_bytes": upload_bytes,
        "media_seconds": math.ceil(media_seconds),
    }
    messages = {
        "operations": "Limite diário de salvamentos e novas análises atingido.",
        "upload_bytes": "Limite diário de arquivos enviados atingido.",
        "media_seconds": "Limite diário de áudio e vídeo para análise atingido.",
    }
    for key, amount in changes.items():
        if old.get(key, 0) + amount > caps[key]:
            raise exceeded(
                messages[key] + " Consulte Uso e privacidade para ver a renovação.", reset, now
            )
    tx.set(
        doc,
        {key: old.get(key, 0) + value for key, value in changes.items()}
        | {"expires_at": reset + timedelta(days=30)},
        merge=True,
    )


def snapshot(db, uid, settings, now=None):
    require_open(db, uid)
    now, reset = window(now)
    old = document(db, uid, now).get().to_dict() or {}
    caps = limits(settings)
    return {
        "resets_at": reset,
        "counters": {key: {"used": old.get(key, 0), "limit": value} for key, value in caps.items()},
    }
