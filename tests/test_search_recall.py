"""Ranked search window: generous tail, contradiction filter on the head only."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from google.cloud.firestore_v1.vector import Vector

from queep.ai import SPACE
from queep.config import database
from queep.models import Interpretation
from queep.ranking import HEAD_LIMIT, MIN_RESULTS, contradicting_ids
from queep.repository import Repository
from queep.search import search

QUERY = [1.0] + [0.0] * 767


def _vector(second):
    return [1.0, second] + [0.0] * 766


def _weak():
    return [0.05, 1.0] + [0.0] * 766


def put_ready(repo, uid, ref_id, vector, *, summary="Interface de CRM", appearance=None,
              kind="note", url=None):
    now = datetime.now(UTC)
    analysis = Interpretation(summary=summary, coverage="full", appearance=appearance or [])
    repo.collection(uid).document(ref_id).set({
        "id": ref_id, "kind": kind, "title": ref_id, "text": summary,
        "url": url, "status": "ready", "deleted": False, "revision": 1,
        "created_at": now, "updated_at": now, "space_ids": [],
        "analysis": analysis.model_dump(), "embedding": Vector(vector),
        "embedding_space": SPACE, "archived_at": None,
    })


def _search(repo, uid, query, verify):
    ai = SimpleNamespace(budget=SimpleNamespace(rate_limit=lambda _: None),
                         embed=lambda *_args, **_kwargs: QUERY, verify=verify)
    return search(repo, ai, uid, query)


def test_broad_query_returns_the_ranked_window_not_a_short_subset():
    repo, uid = Repository(database()), uuid4().hex
    for index in range(20):
        put_ready(repo, uid, f"item-{index:02d}", _vector(index / 1000))
    seen = []

    def verify(_query, refs):
        seen.extend(ref.id for ref in refs)
        return [ref.id for ref in refs]

    result = _search(repo, uid, "interface", verify)
    assert seen == [f"item-{index:02d}" for index in range(HEAD_LIMIT)]
    assert [item.id for item in result.items] == [f"item-{index:02d}" for index in range(20)]
    assert result.candidate_limit == 60


def test_checker_subset_does_not_hide_the_unverified_tail():
    repo, uid = Repository(database()), uuid4().hex
    for index in range(20):
        put_ready(repo, uid, f"item-{index:02d}", _vector(index / 1000))

    def verify(_query, refs):
        return [refs[0].id]

    result = _search(repo, uid, "interface", verify)
    ids = [item.id for item in result.items]
    assert ids[0] == "item-00"
    assert ids[1:] == [f"item-{index:02d}" for index in range(HEAD_LIMIT, 20)]
    assert "item-01" not in ids


def test_tail_cut_keeps_the_minimum_when_scores_fall_off():
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "best", QUERY)
    for index in range(15):
        put_ready(repo, uid, f"weak-{index:02d}", _weak())

    def verify(_query, refs):
        return [ref.id for ref in refs]

    result = _search(repo, uid, "interface", verify)
    assert [item.id for item in result.items][0] == "best"
    assert len(result.items) == MIN_RESULTS


def test_wrong_color_drops_from_the_head_and_the_tail_stays():
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "purple", QUERY, summary="Landing roxa", appearance=["fundo roxo"])
    put_ready(repo, uid, "green", _vector(0.02), summary="Landing", appearance=["botão verde"])
    for index in range(10):
        put_ready(repo, uid, f"tail-{index:02d}", _vector(0.2 + index / 100),
                  summary="Outra página", appearance=["faixa verde"])

    def verify(_query, refs):
        return [ref.id for ref in refs]

    result = _search(repo, uid, "web design roxo", verify)
    ids = [item.id for item in result.items]
    assert "purple" in ids and "green" not in ids
    assert any(item.startswith("tail-") for item in ids)


def test_only_videos_drops_a_note_and_keeps_youtube():
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "note", QUERY, summary="Uma nota")
    put_ready(repo, uid, "youtube", _vector(0.01), summary="Demo", kind="link",
              url="https://www.youtube.com/watch?v=M7lc1UVf-VE")
    put_ready(repo, uid, "page", _vector(0.02), summary="Site", kind="link",
              url="https://example.com/artigo")

    def verify(_query, refs):
        return [ref.id for ref in refs]

    ids = [item.id for item in _search(repo, uid, "somente vídeos", verify).items]
    assert ids == ["youtube"]


def test_color_contradiction_is_local_and_ignores_items_without_a_color():
    now = datetime.now(UTC)

    def ref(identifier, appearance):
        return SimpleNamespace(
            id=identifier, kind="link", url="https://example.com",
            analysis=Interpretation(summary="Página", coverage="full", appearance=appearance),
            created_at=now,
        )

    dropped = contradicting_ids("logos verdes", [
        ref("green", ["marca verde"]),
        ref("purple", ["fundo roxo"]),
        ref("plain", []),
    ])
    assert dropped == ["purple"]

