"""Ranked search window: generous tail, contradiction filter on the head only."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from google.cloud.firestore_v1.vector import Vector

from queep.ai import SPACE
from queep.config import database
from queep.lexicon import expand_terms
from queep.models import Interpretation
from queep.ranking import (
    FUSED_PROTECT,
    HEAD_LIMIT,
    MIN_RESULTS,
    bm25_ranks,
    contradicting_ids,
    order_ready,
)
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


def test_broad_query_keeps_later_cosine_neighbors():
    """A short theme must not stop at the minimum when cosine still has matches."""
    rows = [_record("keyword", _weak(), analysis={"summary": "Interface rara"})]
    for index in range(15):
        rows.append(_record(
            f"near-{index:02d}", _vector(index / 1000), analysis={"summary": "Paisagem"}))
    ordered = [item["id"] for item in order_ready(rows, QUERY, "interface")]
    assert ordered[0] == "keyword"
    assert ordered[:FUSED_PROTECT][0] == "keyword"
    assert len(ordered) == 16
    assert "near-14" in ordered


def test_two_word_theme_is_broad_and_a_third_word_is_not():
    rows = [_record(
        "best", QUERY, analysis={"summary": "Web design de um site com hero e website"})]
    for index in range(15):
        rows.append(_record(
            f"weak-{index:02d}", _weak(), analysis={"summary": "Paisagem sem termo"}))
    broad = [item["id"] for item in order_ready(rows, QUERY, "web design")]
    longer = [item["id"] for item in order_ready(rows, QUERY, "site hero website")]
    assert broad[0] == "best" and len(broad) == 16
    assert longer[0] == "best" and len(longer) == MIN_RESULTS


def test_tail_cut_keeps_the_minimum_when_scores_fall_off():
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "best", QUERY, summary="Landing roxa", appearance=["fundo roxo"])
    for index in range(15):
        put_ready(repo, uid, f"weak-{index:02d}", _weak(), summary="Paisagem sem termo")

    def verify(_query, refs):
        return [ref.id for ref in refs]

    result = _search(repo, uid, "roxo", verify)
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


def _record(identifier, vector, **fields):
    analysis = fields.pop("analysis", {})
    source = fields.pop("source", None)
    record = {"id": identifier, "embedding": vector, "analysis": analysis, "archived_at": None,
              "title": "", "text": ""}
    record.update(fields)
    if source is not None:
        record["source"] = source
    return record


def test_appearance_color_outranks_a_closer_vector():
    purple = _record(
        "purple", _weak(), analysis={"appearance": ["fundo roxo"], "summary": "Landing"})
    closer = _record("closer", QUERY, analysis={"summary": "Outra página sem a cor"})
    ordered = [item["id"] for item in order_ready([closer, purple], QUERY, "roxo")]
    assert ordered[0] == "purple"


def test_caption_term_is_found_and_weighs_less_than_the_title():
    titled = _record("titled", _weak(), title="Paywall do app")
    caption = _record(
        "caption", _weak(), source={"text": "The screen is a paywall after onboarding"})
    noise = _record("noise", QUERY, analysis={"summary": "Paisagem"})
    ranks = bm25_ranks([titled, caption, noise], "paywall")
    assert ranks[0] == "titled" and "caption" in ranks and "noise" not in ranks
    fused = [item["id"] for item in order_ready([noise, caption], QUERY, "paywall")]
    assert fused[0] == "caption"


def test_short_tokens_accents_and_plurals_match():
    rows = [
        _record("web", _weak(), analysis={"topics": ["web design"]}),
        _record("sites", _weak(), title="Websites for brands"),
        _record("purple", _weak(), analysis={"appearance": ["Roxo intenso"]}),
    ]
    assert bm25_ranks(rows, "web")[0] == "web"
    assert "sites" in bm25_ranks(rows, "website")
    assert bm25_ranks(rows, "roxó") == ["purple"]


def test_curated_expansion_maps_design_vocabulary_both_ways():
    web = expand_terms("web design")
    assert "website" in web and "landing page" in web and "página" in web
    assert "purple" in expand_terms("roxo") and "roxo" in expand_terms("purple")
    assert "iphone" in expand_terms("mobile")
    assert "sign in" in expand_terms("login")
    assert "web design" not in web


def test_expansion_boosts_rank_without_dropping_other_items():
    dashboard = _record("dashboard", _weak(), analysis={"topics": ["dashboard"]})
    closer = _record("closer", QUERY, analysis={"summary": "Paisagem"})
    ordered = order_ready(
        [closer, dashboard], QUERY, "interface", expand_terms("interface"))
    assert [item["id"] for item in ordered] == ["dashboard", "closer"]


def test_rewrite_flag_off_is_the_default_and_failure_does_not_break_search(monkeypatch):
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "only", QUERY, summary="Interface de CRM")
    calls = []

    def rewrite(query):
        calls.append(query)
        raise RuntimeError("rewrite down")

    ai = SimpleNamespace(budget=SimpleNamespace(rate_limit=lambda _: None),
                         embed=lambda *_args, **_kwargs: QUERY,
                         verify=lambda _query, refs: [ref.id for ref in refs],
                         rewrite_query=rewrite)
    assert search(repo, ai, uid, "interface").items[0].id == "only"
    assert calls == []
    monkeypatch.setenv("QUEEP_QUERY_REWRITE", "true")
    assert search(repo, ai, uid, "interface").items[0].id == "only"
    assert calls == ["interface"]


def test_rewrite_query_keeps_terms_and_swallows_a_bad_reply():
    from tests.test_ai_processing import fake_client

    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": '{"terms":["tela","dashboard"]}'}}]})
    assert ai.rewrite_query("interface") == ["tela", "dashboard"]
    assert calls[0]["max_tokens"] == 120
    broken, _, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": "not-json"}}]})
    assert broken.rewrite_query("interface") == []
