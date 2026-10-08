"""Select saved links that still need a fetch.

Dry-run is the default. ``--enqueue`` writes the worker outbox
(``jobs`` or ``jobs_testflight``) and sets ``status=queued``. It does not
call Cloud Tasks. The scheduler ``queep-ai-dispatch`` delivers the jobs.

``--enqueue`` is refused unless ``--require-version`` equals the serving
``ANALYSIS_VERSION``. This is not ``queep/backfill.py``. PR #9 should keep
that module for reinterpretation and call :func:`queep.refetch.classify`
when ``--scope fetch`` is requested, so these cohorts stay in one place.

Cohorts, in order: error, unavailable source, author-only Instagram,
video or GIF without motion, then any other ``media_incomplete`` row.
``--exclude-host`` drops a host. Dribbble is not in the default list.
"""

import argparse
import sys
import time
from datetime import UTC, datetime

from queep.ai import ANALYSIS_VERSION
from queep.refetch import COHORTS, classify

PROVIDER_REANALYZE_USD = 0.0035
PROVIDER_VIDEO_USD = 0.010
RESERVE_USD = 0.01
DEFAULT_MAX_COST_USD = 0.50
DEFAULT_RATE_SECONDS = 1.0
_VIDEO_HOSTS = (
    "instagram.com", "youtube.com", "youtu.be", "vimeo.com", "x.com", "twitter.com",
)


def provider_usd(record):
    if record.get("kind") in {"audio", "video"} or record.get("media"):
        return PROVIDER_VIDEO_USD
    url = (record.get("url") or "").lower()
    if any(host in url for host in _VIDEO_HOSTS):
        return PROVIDER_VIDEO_USD
    from queep.refetch import video_without_motion

    if video_without_motion(record):
        return PROVIDER_VIDEO_USD
    return PROVIDER_REANALYZE_USD


def reserve_usd(record):
    calls = 4 if provider_usd(record) == PROVIDER_VIDEO_USD else 2
    return RESERVE_USD * calls


def plan(rows, exclude_hosts, *, limit, max_cost):
    ranked = []
    order = {name: index for index, name in enumerate(COHORTS)}
    for uid, record in rows:
        cohort = classify(record, exclude_hosts)
        if not cohort:
            continue
        ranked.append({
            "uid": uid,
            "id": record.get("id"),
            "revision": record.get("revision"),
            "cohort": cohort,
            "provider_usd": provider_usd(record),
            "reserve_usd": reserve_usd(record),
            "record": record,
        })
    ranked.sort(key=lambda item: (order[item["cohort"]], item["uid"], item["id"] or ""))
    selected, spent, reserved = [], 0.0, 0.0
    stopped = None
    for item in ranked:
        if limit is not None and len(selected) >= limit:
            stopped = stopped or "limit"
            break
        if spent + item["provider_usd"] > max_cost + 1e-9:
            stopped = stopped or "cap"
            break
        selected.append(item)
        spent += item["provider_usd"]
        reserved += item["reserve_usd"]
    return ranked, selected, spent, reserved, stopped


def format_report(ranked, selected, spent, reserved, stopped, mode):
    counts = {name: 0 for name in COHORTS}
    for item in selected:
        counts[item["cohort"]] += 1
    lines = [
        f"mode: {mode}",
        "scope: fetch",
        f"version: {ANALYSIS_VERSION}",
        f"eligible: {len(ranked)}",
        f"selected: {len(selected)}",
        f"estimated_provider_usd: {spent:.4f}",
        f"reserve_usd: {reserved:.4f}",
        "cohorts: " + ", ".join(f"{name}={counts[name]}" for name in COHORTS),
        f"stopped: {stopped or 'no'}",
    ]
    for item in selected:
        lines.append(
            f"- {item['cohort']} {item['uid']} {item['id']} US$ {item['provider_usd']:.4f}"
        )
    return "\n".join(lines)


def enqueue_one(repo, item, now):
    record = item["record"]
    ref_id = record["id"]
    doc = repo.collection(item["uid"]).document(ref_id)
    current = doc.get().to_dict() or {}
    if current.get("deleted") or current.get("status") in {"queued", "processing"}:
        return False
    if current.get("revision") != record.get("revision"):
        return False
    from queep.repository import new_job

    doc.update({
        "status": "queued",
        "updated_at": now,
        "media_incomplete": False,
        "fetch_retry_at": now,
    })
    repo.job(item["uid"], ref_id).set(new_job(
        item["uid"], ref_id, record["revision"], now) | {"plan": "reprocess", "scope": "fetch"})
    return True


def load_rows(db, uid):
    owners = [uid] if uid else [doc.id for doc in db.collection("users").stream()]
    rows = []
    for owner in owners:
        collection = db.collection("users").document(owner).collection("references")
        for doc in collection.stream():
            data = doc.to_dict() or {}
            data["id"] = data.get("id") or doc.id
            rows.append((owner, data))
    return rows


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--scope", default="fetch", choices=["fetch"])
    parser.add_argument("--require-version", default=None)
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_SECONDS)
    parser.add_argument("--exclude-host", action="append", default=[])
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.max_cost_usd < 0:
        parser.error("--max-cost-usd must be >= 0")
    if args.rate < 0:
        parser.error("--rate must be >= 0")
    return args


def main(argv=None, *, sleep=time.sleep, now=None):
    args = parse_args(argv)
    if args.enqueue and args.require_version != ANALYSIS_VERSION:
        print(
            "--require-version must equal the serving worker " + ANALYSIS_VERSION,
            file=sys.stderr,
        )
        return 2
    from queep.config import database
    from queep.repository import Repository

    db = database()
    rows = load_rows(db, args.uid)
    ranked, selected, spent, reserved, stopped = plan(
        rows, tuple(args.exclude_host), limit=args.limit, max_cost=args.max_cost_usd)
    mode = "enqueue" if args.enqueue else "dry-run"
    if args.enqueue:
        repo = Repository(db)
        clock = now or (lambda: datetime.now(UTC))
        for index, item in enumerate(selected):
            if index and args.rate:
                sleep(args.rate)
            enqueue_one(repo, item, clock())
    print(format_report(ranked, selected, spent, reserved, stopped, mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
