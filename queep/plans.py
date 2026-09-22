"""Commercial policy. Prices belong to StoreKit; quotas belong to the server."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException

Plan = Literal["free", "pro", "max"]
MIB = 1024 * 1024
LIMITS = {
    "free": dict(operations=12, searches=8, media_seconds=0, items=100,
                 storage_bytes=250 * MIB, upload_bytes=80 * MIB, spaces=0),
    "pro": dict(operations=100, searches=50, media_seconds=0, items=800,
                storage_bytes=2048 * MIB, upload_bytes=400 * MIB, spaces=15),
    "max": dict(operations=200, searches=80, media_seconds=2400, items=2500,
                storage_bytes=10240 * MIB, upload_bytes=6144 * MIB, spaces=40),
}


def month_window(now=None):
    now = (now or datetime.now(UTC)).astimezone(UTC)
    reset = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return now, reset


def features(plan):
    return {"spaces": plan != "free", "media_av": plan == "max"}


def denied(plan, resource, *, now=None):
    _, reset = month_window(now)
    code = ("media_av_required" if resource == "media_av" else
            "storage_limit" if resource == "storage_bytes" else "plan_limit")
    target = "max" if resource == "media_av" or plan == "pro" else (
        "pro" if plan == "free" else None)
    message = ("Áudio e vídeo são do Queep Max." if resource == "media_av" else
               "Spaces estão disponíveis a partir do Queep Pro." if resource == "spaces" else
               "Armazenamento do plano atingido. Exclua arquivos ou altere seu plano."
               if resource == "storage_bytes" else
               "Limite do plano atingido. Confira seu uso e a data de renovação.")
    return HTTPException(402, {"code": code, "message": message, "plan": plan,
                               "resource": resource, "required_plan": target,
                               "resets_at": reset.isoformat()})


def require_space_change(plan, current_count, *, creating=False):
    """GAZ-116 integration point: assigning remains allowed at/above a paid plan's cap."""
    if plan == "free" or (creating and current_count >= LIMITS[plan]["spaces"]):
        raise denied(plan, "spaces")
