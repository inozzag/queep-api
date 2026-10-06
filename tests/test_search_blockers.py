"""Contract tests for the three natural-language search blockers."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from google.cloud.firestore_v1.vector import Vector

from queep.ai import SPACE
from queep.config import database
from queep.models import Interpretation
from queep.repository import Repository
from queep.search import NEIGHBOR_LIMIT, search

ANALYSIS = Interpretation(summary="Interface de CRM branca", coverage="full")
QUERY = [1.0] + [0.0] * 767
_OMIT = object()


def put_ready(repo, uid, ref_id, vector, *, archived_at=_OMIT, kind="note", url=None):
    now = datetime.now(UTC)
    payload = {
        "id": ref_id, "kind": kind, "title": "", "text": "Interface de CRM branca",
        "url": url, "status": "ready", "deleted": False, "revision": 1,
        "created_at": now, "updated_at": now, "space_ids": [],
        "analysis": ANALYSIS.model_dump(), "embedding": Vector(vector),
        "embedding_space": SPACE,
    }
    if archived_at is not _OMIT:
        payload["archived_at"] = archived_at
    repo.collection(uid).document(ref_id).set(payload)


def test_archived_rows_do_not_consume_neighbor_slots():
    repo = Repository(database())
    uid = uuid4().hex
    # Closer than every live row, and enough of them to fill the old shortlist by themselves.
    archived_vector = QUERY
    for index in range(NEIGHBOR_LIMIT):
        put_ready(repo, uid, f"archived-{index:02d}", archived_vector,
                  archived_at=datetime.now(UTC))
    note_vector = [0.2, 1.0] + [0.0] * 766
    link_vector = [0.05, 0.0, 1.0] + [0.0] * 765
    put_ready(repo, uid, "live-note", note_vector, archived_at=None)
    # Image and media uploads omit archived_at; that still means active.
    put_ready(repo, uid, "live-link", link_vector, kind="link",
              url="https://www.youtube.com/watch?v=M7lc1UVf-VE")
    seen = []

    def verify(query, refs):
        seen.extend(ref.id for ref in refs)
        return [ref.id for ref in refs]

    ai = SimpleNamespace(budget=SimpleNamespace(rate_limit=lambda _: None),
                         embed=lambda *_args, **_kwargs: QUERY, verify=verify)
    result = search(repo, ai, uid, "Interface de CRM")
    assert seen == ["live-note", "live-link"]
    assert [item.id for item in result.items] == ["live-note", "live-link"]
    assert not any(item.id.startswith("archived-") for item in result.items)
