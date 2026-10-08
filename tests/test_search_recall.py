"""Ranked search window: generous tail, contradiction filter on the head only."""

import math
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from google.cloud.firestore_v1.vector import Vector

from queep.ai import SPACE
from queep.config import database
from queep.lexicon import expand_terms
from queep.models import Interpretation
from queep.ranking import (
    BROAD_NONLEX_LIMIT,
    BROAD_RESULT_CAP,
    FUSED_PROTECT,
    HEAD_LIMIT,
    MIN_RESULTS,
    SPECIFIC_MISS_LIMIT,
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


def _at_cosine(score):
    """Vector whose cosine with QUERY is `score`. QUERY is the unit axis."""
    score = min(0.999, max(1e-3, score))
    other = math.sqrt(1.0 / (score * score) - 1.0)
    return [1.0, other] + [0.0] * 766


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


def test_broad_query_keeps_a_few_strong_cosine_neighbors():
    """Lexical row stays first. Only neighbors above this shelf's mean + std come along."""
    rows = []
    for index in range(57):
        score = 0.684 - (0.684 - 0.538) * (index / 56) ** 0.5
        summary = "Interface rara" if index == 20 else "Paisagem"
        rows.append(_record(f"row-{index:02d}", _at_cosine(score), analysis={"summary": summary}))
    ordered = [item["id"] for item in order_ready(rows, QUERY, "interface")]
    assert ordered[0] == "row-20"
    assert "row-00" in ordered
    assert "row-56" not in ordered
    assert len(ordered) <= 1 + BROAD_NONLEX_LIMIT


def test_two_word_theme_is_broad_and_a_third_word_is_not():
    rows = [_record(
        "best", QUERY, analysis={"summary": "Web design de um site com hero e website"})]
    for index in range(15):
        rows.append(_record(
            f"weak-{index:02d}", _weak(), analysis={"summary": "Paisagem sem termo"}))
    broad = [item["id"] for item in order_ready(rows, QUERY, "web design")]
    longer = [item["id"] for item in order_ready(rows, QUERY, "site hero website")]
    assert broad == ["best"]
    assert longer[0] == "best" and len(longer) == MIN_RESULTS


def test_broad_floor_keeps_the_cluster_and_drops_the_shelf():
    """A real theme stays in the relevant band. The orthogonal tail does not refill it."""
    rows = []
    for index in range(32):
        rows.append(_record(
            f"ui-{index:02d}", _at_cosine(0.97 - index * 0.001),
            analysis={"summary": "Interface de produto"}))
    for index in range(25):
        rows.append(_record(
            f"far-{index:02d}", _at_cosine(0.20),
            analysis={"summary": "Paisagem sem termo"}))
    ordered = [item["id"] for item in order_ready(rows, QUERY, "interface")]
    assert ordered[:FUSED_PROTECT] == [f"ui-{index:02d}" for index in range(FUSED_PROTECT)]
    assert "ui-20" in ordered
    assert "far-00" not in ordered
    assert 20 <= len(ordered) <= BROAD_RESULT_CAP
    assert len(ordered) < len(rows)


def test_flat_theme_caps_instead_of_returning_the_shelf():
    rows = [
        _record(f"ui-{index:02d}", _at_cosine(0.95), analysis={"summary": "Interface de produto"})
        for index in range(57)
    ]
    ordered = [item["id"] for item in order_ready(rows, QUERY, "interface")]
    assert len(ordered) == BROAD_RESULT_CAP
    assert ordered[:FUSED_PROTECT] == [f"ui-{index:02d}" for index in range(FUSED_PROTECT)]
    assert "ui-56" not in ordered


def test_broad_head_keeps_a_weak_keyword_and_a_few_close_neighbors():
    rows = [_record("keyword", _at_cosine(0.05), analysis={"summary": "Interface rara"})]
    rows.extend(
        _record(f"near-{index:02d}", _at_cosine(0.96 - index * 0.001),
                analysis={"summary": "Paisagem"})
        for index in range(20))
    rows.extend(
        _record(f"far-{index:02d}", _at_cosine(0.12), analysis={"summary": "Outro"})
        for index in range(36))
    ordered = [item["id"] for item in order_ready(rows, QUERY, "interface")]
    assert ordered[0] == "keyword"
    assert ordered[1] == "near-00"
    assert "near-14" not in ordered
    assert "far-00" not in ordered
    assert len(ordered) == 1 + BROAD_NONLEX_LIMIT


def test_login_on_a_bunched_shelf_stays_near_the_lexical_count():
    """Live shape: best .674, worst .549, five items actually say login, chrome does not count."""
    rows = []
    for index in range(57):
        score = 0.674 - (0.674 - 0.549) * index / 56
        summary = "Tela de login" if index < 5 else "Interface de produto"
        rows.append(_record(
            f"row-{index:02d}", _at_cosine(score),
            analysis={"summary": summary},
            source={"text": "Log in Sign up"},
        ))
    ordered = [item["id"] for item in order_ready(rows, QUERY, "login", expand_terms("login"))]
    assert all(f"row-{index:02d}" in ordered for index in range(5))
    assert len(ordered) <= 12
    assert len(ordered) <= 5 + BROAD_NONLEX_LIMIT
    assert "row-56" not in ordered


def test_bunched_shelf_tracks_lexical_evidence():
    """Lengths from the live best/worst bands: few matches stay short, themes stay under 40."""
    bands = [
        ("paywall", 0.680, 0.545, 51, 9, 9, 15),
        ("mobile", 0.690, 0.565, 56, 15, 15, 20),
        ("interface", 0.684, 0.538, 52, 28, 28, 40),
        ("web design", 0.761, 0.578, 48, 38, 30, 40),
        ("website", 0.740, 0.579, 51, 38, 30, 40),
    ]
    for query, best, worst, n_high, lexical, low, high in bands:
        floor = 0.82 * best
        rows = []
        for index in range(57):
            if index < n_high:
                score = best - (best - floor) * index / max(n_high - 1, 1)
            else:
                score = floor - (floor - worst) * (index - n_high + 1) / max(57 - n_high, 1)
            summary = f"{query} de referência" if index < lexical else "Paisagem sem termo"
            rows.append(_record(
                f"row-{index:02d}", _at_cosine(score), analysis={"summary": summary}))
        ordered = [item["id"] for item in order_ready(rows, QUERY, query, expand_terms(query))]
        assert low <= len(ordered) <= high, query
        assert len(ordered) < 57
        assert ordered[:FUSED_PROTECT] == [f"row-{index:02d}" for index in range(FUSED_PROTECT)]
        assert all(f"row-{index:02d}" in ordered for index in range(lexical))


def test_cosine_tail_keeps_a_close_lexical_item_ahead_of_a_weak_one():
    """Fused head stays put. After it, cosine order puts the close labeled row forward."""
    rows = [
        _record(f"head-{index:02d}", _at_cosine(0.62 - index * 0.002), title="interface")
        for index in range(10)
    ]
    rows.append(_record("close", _at_cosine(0.90), analysis={"topics": ["ui"]}))
    rows.append(_record("late", _at_cosine(0.50), title="interface"))
    rows.extend(
        _record(f"far-{index:02d}", _at_cosine(0.58), analysis={"summary": "Paisagem"})
        for index in range(30))
    terms = expand_terms("interface")
    ordered = [item["id"] for item in order_ready(rows, QUERY, "interface", terms)]
    assert ordered[:10] == [f"head-{index:02d}" for index in range(10)]
    assert "close" in ordered and "late" in ordered
    assert ordered.index("close") < ordered.index("late")
    assert ordered.index("close") == 10
    assert "far-00" not in ordered


def test_rare_page_term_adds_a_few_neighbors_not_the_shelf():
    rows = []
    for index in range(57):
        score = 0.680 - (0.680 - 0.545) * index / 56
        source = {"text": "The pricing paywall blocks the app"} if index < 3 else None
        rows.append(_record(
            f"row-{index:02d}", _at_cosine(score),
            analysis={"summary": "Tela de produto"}, source=source))
    ordered = [item["id"] for item in order_ready(rows, QUERY, "paywall", expand_terms("paywall"))]
    assert all(f"row-{index:02d}" in ordered for index in range(3))
    assert "row-00" in ordered
    assert "row-56" not in ordered
    assert 3 < len(ordered) <= 3 + BROAD_NONLEX_LIMIT
    assert len(ordered) <= 15


def test_tail_cut_keeps_the_minimum_when_scores_fall_off():
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "best", QUERY, summary="Landing roxa", appearance=["fundo roxo"])
    for index in range(15):
        put_ready(repo, uid, f"weak-{index:02d}", _weak(), summary="Paisagem sem termo")

    def verify(_query, refs):
        return [ref.id for ref in refs]

    result = _search(repo, uid, "roxo", verify)
    assert [item.id for item in result.items] == ["best"]


def test_wrong_color_drops_from_the_head_and_the_tail():
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "purple", QUERY, summary="Landing roxa", appearance=["fundo roxo"])
    put_ready(repo, uid, "green", _vector(0.02), summary="Landing", appearance=["botão verde"])
    for index in range(10):
        put_ready(repo, uid, f"tail-{index:02d}", _vector(0.2 + index / 100),
                  summary="Outra página", appearance=["faixa verde"])

    def verify(_query, refs):
        return [ref.id for ref in refs]

    result = _search(repo, uid, "web design roxo", verify)
    assert [item.id for item in result.items] == ["purple"]


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


def test_color_query_keeps_v8_colors_and_v7_appearance_only():
    v8 = _record("v8", _weak(), analysis={"summary": "Página", "colors": ["roxo / purple"]})
    v7 = _record("v7", _vector(0.05), analysis={
        "summary": "Landing", "appearance": ["fundo roxo"]})
    brand = _record("brand", _vector(0.08), analysis={"summary": "App", "brand": "Azul"})
    caption = _record("caption", QUERY, source={"text": "a green paywall"}, analysis={
        "summary": "Outra página"})
    fillers = [
        _record(f"logo-{index:02d}", _vector(0.2 + index / 100), analysis={"summary": "Logo"})
        for index in range(6)
    ]
    purple = [item["id"] for item in order_ready(
        [caption, v8, v7, brand, *fillers], QUERY, "web design roxo")]
    assert set(purple) == {"v8", "v7"}
    blue = [item["id"] for item in order_ready(
        [caption, v8, v7, brand, *fillers], QUERY, "carro app logo azul")]
    assert blue == ["brand"]


def test_unmatched_color_returns_a_short_near_miss_list():
    rows = [
        _record(f"logo-{index:02d}", _vector(index / 100), analysis={
            "summary": "Logo de uma marca", "ui_types": ["logo"]})
        for index in range(20)
    ]
    ordered = [item["id"] for item in order_ready(rows, QUERY, "logos verdes")]
    assert ordered == [f"logo-{index:02d}" for index in range(SPECIFIC_MISS_LIMIT)]
    assert len(ordered) < MIN_RESULTS


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
