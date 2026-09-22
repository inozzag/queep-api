"""30-day provider cost export, grouped by logical operation instead of individual calls."""
from collections import defaultdict


def summarize(records):
    operations = {}
    unknown = 0
    for record in records:
        key = record.get("operation_id")
        if not key or record.get("plan") not in ("free", "pro", "max"):
            unknown += 1
            continue
        operation = operations.setdefault(key, {
            "plan": record["plan"], "kind": record.get("kind", "unknown"),
            "cost": 0, "complete": True, "seconds": record.get("media_seconds", 0),
        })
        if record.get("cost_usd") is None:
            operation["complete"] = False
        else:
            operation["cost"] += record["cost_usd"]
    groups = defaultdict(list)
    for operation in operations.values():
        groups[operation["plan"], operation["kind"]].append(operation)
    rows = []
    for (plan, kind), values in sorted(groups.items()):
        known = [value for value in values if value["complete"]]
        cost = sum(v["cost"] for v in known)
        minutes = sum(v["seconds"] for v in known) / 60
        average = cost / len(known) if known else None
        per_minute = cost / minutes if minutes else None
        threshold = .022 if kind == "social" else .06 if kind == "media" else .015
        observed = per_minute if kind == "media" else average
        rows.append({"plan": plan, "kind": kind, "operations": len(values),
                     "operations_with_known_cost": len(known), "provider_cost_usd": cost,
                     "average_capture_usd": average, "average_media_minute_usd": per_minute,
                     "review_quotas": observed is not None and observed > threshold})
    return {"groups": rows, "unattributed_calls": unknown,
            "basis": "Provider costs only. Add compute, extraction, storage and egress for COGS."}
