"""Contract tests for the three natural-language search blockers."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from google.cloud.firestore_v1.vector import Vector

from queep.ai import SPACE, AIError
from queep.budget import Budget
from queep.config import database
from queep.main import app
from queep.models import Interpretation, Reference
from queep.repository import Repository
from queep.search import NEIGHBOR_LIMIT, search
from queep.usage import daily_document, document
from queep.worker import engine
from tests.test_ai_processing import fake_client

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


def reference(identifier, kind, url=None):
    now = datetime.now(UTC)
    return Reference(id=identifier, kind=kind, title="", text="cena azul", url=url,
                     status="ready", revision=1, created_at=now, updated_at=now)


LINKS = (
    reference("youtube", "link", "https://www.youtube.com/watch?v=M7lc1UVf-VE"),
    reference("page", "link", "https://example.com/artigo"),
    reference("post", "link", "https://x.com/ada/status/1"),
)
LIBRARY = (
    *LINKS,
    reference("video", "video"),
    reference("image", "image"),
    reference("audio", "audio"),
    reference("note", "note"),
)
SELECTED = [item.id for item in LIBRARY]


@pytest.mark.parametrize("requested,expected", [
    ("video", ["youtube", "page", "post", "video"]),
    ("image", ["youtube", "page", "post", "image"]),
    ("audio", ["audio"]),
    ("note", ["note"]),
    ("link", ["youtube", "page", "post"]),
    ("any", SELECTED),
])
def test_visual_and_video_queries_keep_posts_pages_and_youtube(requested, expected):
    ai, _, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"requested_kind": requested, "ids": SELECTED})}}]})
    assert ai.verify("mostre o material", LIBRARY) == expected


def _usage(uid, now):
    return document(database(), uid, now).get().to_dict() or {}


def test_verifier_failure_returns_the_ranked_list_without_spending(client, account):
    uid, headers = account
    repo = Repository(database())
    put_ready(repo, uid, "live-note", QUERY, archived_at=None)
    now = datetime.now(UTC)
    document(database(), uid, now).set({"searches": 3}, merge=True)

    def verify(_query, _refs):
        raise AIError("invalid_selection")

    ai = SimpleNamespace(budget=Budget(database(), 1), embed=lambda *_a, **_k: QUERY,
                         verify=verify)
    app.dependency_overrides[engine] = lambda: ai
    try:
        response = client.post("/v1/search", headers=headers, json={"query": "CRM branco"})
    finally:
        app.dependency_overrides.pop(engine, None)
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == ["live-note"]
    stored = _usage(uid, now)
    assert stored.get("searches") == 3
    assert not stored.get("pending_searches")
    assert (daily_document(database(), uid, now).get().to_dict() or {}).get("searches", 0) == 0


def test_successful_search_still_debits_one_monthly_search(client, account):
    uid, headers = account
    repo = Repository(database())
    put_ready(repo, uid, "live-note", QUERY, archived_at=None)
    now = datetime.now(UTC)
    document(database(), uid, now).set({"searches": 3}, merge=True)

    def verify(_query, refs):
        return [ref.id for ref in refs]

    ai = SimpleNamespace(budget=Budget(database(), 1), embed=lambda *_a, **_k: QUERY,
                         verify=verify)
    app.dependency_overrides[engine] = lambda: ai
    try:
        response = client.post("/v1/search", headers=headers, json={"query": "CRM branco"})
    finally:
        app.dependency_overrides.pop(engine, None)
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == ["live-note"]
    stored = _usage(uid, now)
    assert stored.get("searches") == 4
    assert not stored.get("pending_searches")


def test_releasing_one_search_does_not_refund_another(account):
    uid, _ = account
    budget = Budget(database(), 1)
    now = datetime.now(UTC)
    document(database(), uid, now).set({"searches": 3}, merge=True)
    first = budget.rate_limit(uid)
    document(database(), uid, now).update(
        {"last_search": datetime.now(UTC) - timedelta(seconds=2)})
    second = budget.rate_limit(uid)
    budget.release_search(uid, first)
    stored = _usage(uid, now)
    assert stored["searches"] == 4
    assert [item["id"] for item in stored["pending_searches"]] == [second]
    budget.commit_search(uid, second)
    budget.release_search(uid, second)
    settled = _usage(uid, now)
    assert settled["searches"] == 4
    assert not settled.get("pending_searches")
