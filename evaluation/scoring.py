"""Deterministic retrieval metrics; labels never travel to the interpretation model."""

import math


def cosine(left, right):
    if len(left) != len(right) or not left:
        raise ValueError("Incompatible vectors")
    norm = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    if not norm:
        raise ValueError("Zero vector")
    return sum(x*y for x, y in zip(left, right, strict=True)) / norm


def rank(vectors, query):
    return sorted(((identifier, cosine(vector, query)) for identifier, vector in vectors.items()),
                  key=lambda pair: (-pair[1], pair[0]))


def evaluate(queries, rankings, threshold):
    rows = []
    for query in queries:
        ranked = rankings[query["id"]]
        expected = set(query["expected"])
        raw_top5 = [identifier for identifier, _ in ranked[:5]]
        selected = [identifier for identifier, score in ranked if score >= threshold]
        top5 = selected[:5]
        hits = len(expected.intersection(selected))
        rows.append({"id": query["id"], "mode": query["mode"],
                     "expected": sorted(expected), "top5": top5, "selected": selected,
                     "hit_at_5": bool(expected.intersection(top5)) if expected else None,
                     "raw_rank_hit_at_5": bool(expected.intersection(raw_top5))
                     if expected else None,
                     "precision": hits / len(selected) if selected else 0.0,
                     "recall": hits / len(expected) if expected else None,
                     "false_positive": bool(selected) if not expected else None})
    known = [row for row in rows if row["mode"] == "known"]
    sets = [row for row in rows if row["mode"] == "set"]
    empty = [row for row in rows if row["mode"] == "empty"]

    def average(group, key):
        return sum(row[key] for row in group) / len(group) if group else None

    return {"known_hit_at_5": average(known, "hit_at_5"),
            "raw_rank_hit_at_5": average(known, "raw_rank_hit_at_5"),
            "set_precision": average(sets, "precision"), "set_recall": average(sets, "recall"),
            "empty_false_positive_rate": average(empty, "false_positive"),
            "known_count": len(known), "set_count": len(sets), "empty_count": len(empty),
            "successful_known_queries": sum(row["hit_at_5"] for row in known), "queries": rows}


def calibrate(queries, rankings):
    """Choose threshold ONLY on calibration queries; prefer higher threshold on ties."""
    if not queries or any(query["split"] != "calibration" for query in queries):
        raise ValueError("Calibration queries required; test labels must not tune the threshold")
    best = (-1.0, 1.01)
    for step in range(-100, 102):
        threshold = step / 100
        rows = evaluate(queries, rankings, threshold)["queries"]
        utilities = []
        for row in rows:
            if row["mode"] == "empty":
                utilities.append(float(not row["false_positive"]))
            else:
                precision, recall = row["precision"], row["recall"]
                utilities.append(2 * precision * recall / (precision + recall)
                                 if precision + recall else 0.0)
        best = max(best, (sum(utilities) / len(utilities), threshold))
    return best[1]
