"""Rewrite the embeddings of one library as functional descriptions.

Does not create references, does not call the model twice for the same item, and
does not change revision. An item is left untouched when the new reading fails.
"""

import os
import sys
from datetime import UTC, datetime

HOSTS = ("motionsites", "fontshare", "indiehackers", "instagram.com", "news.ycombinator")


def configure():
    os.environ.setdefault("QUEEP_ENV", "production")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "queep-dev-hstlx")
    os.environ.setdefault("QUEEP_STORAGE_BUCKET", "queep-dev-hstlx.firebasestorage.app")
    os.environ.setdefault("QUEEP_BILLING_MODE", "development")
    os.environ.setdefault("QUEEP_AI_ENABLED", "true")


def library_owner(db):
    chosen = []
    for user in db.collection("users").stream():
        for doc in user.reference.collection("references").limit(80).stream():
            record = doc.to_dict() or {}
            blob = " ".join(str(record.get(key) or "") for key in ("url", "title"))
            if any(host in blob for host in HOSTS):
                chosen.append(user.id)
                break
    return chosen


def frames_for(images, uid, ref):
    gallery = ref.images or ([ref.image] if ref.image else [])
    found = []
    for index in range(min(2, len(gallery))):
        try:
            found.append(images.read(uid, ref.id, thumbnail=True, index=index)[0])
        except Exception:
            continue
    return found


def rewrite(ai, images, uid, ref):
    from queep.ai import document

    picture = frames_for(images, uid, ref)
    analysis = ai.interpret(ref, picture or None)
    vector = ai.embed(document(ref, analysis))
    return analysis, vector


def main():
    configure()
    from google.cloud.firestore_v1.vector import Vector

    from queep.ai import ANALYSIS_VERSION, INTERPRETER, SPACE
    from queep.config import database, image_bucket
    from queep.images import Images
    from queep.models import Reference
    from queep.worker import engine

    db = database()
    owners = library_owner(db)
    if len(owners) != 1:
        print(f"expected one library, found {len(owners)}")
        return 1
    uid = owners[0]
    images = Images(db, image_bucket())
    ai = engine()
    counts = {}
    docs = list(db.collection("users").document(uid).collection("references").stream())
    print(f"rewriting {len(docs)} references")
    for doc in docs:
        record = doc.to_dict() or {}
        if record.get("deleted") or record.get("status") != "ready":
            counts["skip"] = counts.get("skip", 0) + 1
            continue
        if (record.get("embedding_space") == SPACE
                and record.get("analysis_version") == ANALYSIS_VERSION):
            counts["current"] = counts.get("current", 0) + 1
            continue
        try:
            ref = Reference.model_validate(record)
            analysis, vector = rewrite(ai, images, uid, ref)
        except Exception as error:
            counts["failed"] = counts.get("failed", 0) + 1
            host = (record.get("url") or record.get("kind") or "")[:80]
            print(f"  failed {host} {type(error).__name__}")
            continue
        doc.reference.update({
            "analysis": analysis.model_dump(),
            "embedding": Vector(vector),
            "embedding_space": SPACE,
            "analysis_version": ANALYSIS_VERSION,
            "analysis_model": INTERPRETER["id"],
            "updated_at": datetime.now(UTC),
        })
        host = (record.get("url") or record.get("title") or record.get("kind") or "")
        host = host.split("/")[2] if "://" in host else host[:40]
        print(f"  rewritten {host}")
        counts["rewritten"] = counts.get("rewritten", 0) + 1
    print(counts)
    return 0 if not counts.get("failed") else 1


if __name__ == "__main__":
    sys.exit(main())
