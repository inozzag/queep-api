"""Which saved links should be fetched again.

`POST /v1/references/{id}/retry` and `python -m queep.reprocess` share these
rules. PR #9's `queep/backfill.py` should call `classify` for `--scope fetch`
instead of growing a second selector.
"""

import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

FETCH_COOLDOWN = timedelta(minutes=10)
COHORTS = ("error", "unavailable", "author_only", "poster", "media_incomplete")
# Opt-in only (`reprocess --cohort clip`): posts saved before full clips were kept.
CLIP_COHORT = "clip"


def host_of(url):
    if not url:
        return ""
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def excluded_host(record, hosts):
    host = host_of(record.get("url") or "")
    if not host:
        return False
    return any(host == name or host.endswith("." + name) for name in hosts)


def gallery(record):
    images = list(record.get("images") or [])
    if not images and isinstance(record.get("image"), dict):
        return [record["image"]]
    return images


def video_without_motion(record):
    return any(
        isinstance(item, dict)
        and item.get("preview_kind") in ("video", "gif")
        and not item.get("motion")
        for item in gallery(record)
    )


def video_without_clip(record):
    """A post video whose full clip, with its sound, is not stored yet."""
    return any(
        isinstance(item, dict) and item.get("preview_kind") == "video" and not item.get("clip")
        for item in gallery(record)
    )


def source_of(record):
    source = record.get("source")
    return source if isinstance(source, dict) else {}


def unavailable(record):
    return source_of(record).get("state") == "unavailable"


def author_only(record):
    """Instagram metadata with no slide and almost no text."""
    if record.get("kind") != "link":
        return False
    source = source_of(record)
    if source.get("content_kind") != "metadata" or source.get("state") != "read":
        return False
    if gallery(record):
        return False
    if len((source.get("text") or "").strip()) >= 8:
        return False
    return "instagram.com" in (record.get("url") or "").lower()


def eligible_retry(record):
    """error, media_incomplete, an unavailable source, or a video/gif still."""
    status = record.get("status")
    if status == "error":
        return True
    if status != "ready":
        return False
    if record.get("media_incomplete"):
        return True
    if unavailable(record):
        return True
    return video_without_motion(record)


def classify(record, exclude_hosts=(), *, clips=False):
    """Reprocess cohort, or None when the row should be left alone.

    With `clips`, only finished post videos without a stored clip are selected.
    """
    if record.get("deleted") or excluded_host(record, exclude_hosts):
        return None
    status = record.get("status")
    if status in {"queued", "processing"}:
        return None
    if clips:
        if status == "ready" and record.get("kind") == "link" and video_without_clip(record):
            return CLIP_COHORT
        return None
    if status == "error":
        return "error"
    if status != "ready":
        return None
    if unavailable(record):
        return "unavailable"
    if author_only(record):
        return "author_only"
    if video_without_motion(record):
        return "poster"
    if record.get("media_incomplete"):
        return "media_incomplete"
    return None


def cooldown_remaining(stamp, now):
    if not isinstance(stamp, datetime):
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    remaining = FETCH_COOLDOWN.total_seconds() - (now - stamp).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining))


def media_incomplete(reference, *, missed, page, profile):
    """Whether a finished link still needs a fetch.

    Profiles, text-only posts, and pages that simply have no video stay complete.
    A failed download is not flagged for pages.
    """
    if getattr(reference, "kind", None) != "link" or profile:
        return False
    source = reference.source
    if source is not None and source.state == "unavailable":
        return True
    if page:
        return False
    if missed:
        return True
    images = list(reference.images or [])
    if not images and reference.image:
        images = [reference.image]
    if any(image.preview_kind in ("video", "gif") and not image.motion for image in images):
        return True
    text = ((source.text if source else "") or "").strip()
    url = (reference.url or "").lower()
    return bool(
        source and source.content_kind == "metadata" and not images and len(text) < 8
        and "instagram.com" in url
    )
