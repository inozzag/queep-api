"""Owner-query file and offline fixtures for the recall harness."""

from datetime import UTC, datetime

import pytest

from eval.recall import (
    apply_checker,
    fuse_and_cosine,
    load_queries,
    recall_at,
    render_header,
    render_labeled,
)
from queep.ai import AIError
from queep.models import Interpretation
from queep.ranking import BROAD_COSINE_KEEP, FUSED_PROTECT, SPECIFIC_MISS_LIMIT

QUERY = [1.0] + [0.0] * 767
WEAK = [0.05, 1.0] + [0.0] * 766
NOW = datetime(2026, 1, 1, tzinfo=UTC)
EXPECTED = {
    "interface": 31,
    "web design": 23,
    "web design roxo": 4,
    "mobile": 7,
    "paywall": 3,
    "login": 0,
    "website": 31,
    "site hero de website": 11,
    "logos verdes": 0,
    "carro app logo azul": 0,
}


def _row(ref_id, vector, summary="Nota", **fields):
    record = {
        "id": ref_id, "archived_at": None, "embedding": vector,
        "title": "", "text": "", "analysis": {"summary": summary},
    }
    record.update(fields)
    return record


def _saved(ref_id, summary, colors=None):
    return {
        "id": ref_id, "kind": "note", "title": "", "text": summary, "url": None,
        "status": "ready", "revision": 1, "created_at": NOW, "updated_at": NOW,
        "archived_at": None, "embedding": list(QUERY),
        "analysis": Interpretation(summary=summary, coverage="full",
                                 colors=colors or []).model_dump(),
    }


def test_owner_queries_are_ids_only_and_include_the_qualitative_set():
    labeled, qualitative = load_queries()
    assert {item["query"]: len(item["relevant"]) for item in labeled} == EXPECTED
    assert qualitative == [
        "interface de aplicativo de carro que tinha uma logo azul",
        "vídeos que têm hook que gera criatividade",
        "logos verdes",
    ]


def test_recall_header_names_the_adaptive_cut():
    header = render_header()
    assert f"protege={FUSED_PROTECT}" in header
    assert f"cosseno_amplo={BROAD_COSINE_KEEP}" in header
    assert f"sem_cor={SPECIFIC_MISS_LIMIT}" in header


def test_recall_math_and_empty_labels():
    assert recall_at(["a", "b", "c"], ["b", "d"], 10) == 0.5
    assert recall_at(["a"], [], 10) is None
    lines = render_labeled("login", ["only"], ["only", "two"], [])
    assert all("rótulo vazio" in line for line in lines)
    assert "n=1" in lines[0] and "n=2" in lines[1]


def test_offline_fixtures_cover_broad_color_caption_and_expansion():
    broad = [_row(f"item-{index:02d}", [1.0, index / 1000] + [0.0] * 766,
                  summary="Interface de produto") for index in range(16)]
    fused, _cosine = fuse_and_cosine(broad, QUERY, "interface")
    assert len(fused) >= 12

    purple = _row("purple", WEAK, appearance=None)
    purple["analysis"] = {"summary": "Landing", "appearance": ["fundo roxo"]}
    closer = _row("closer", QUERY, summary="Outra página sem a cor")
    assert fuse_and_cosine([closer, purple], QUERY, "roxo")[0][0] == "purple"

    caption = _row("caption", WEAK, source={"text": "The screen is a paywall after onboarding"})
    noise = _row("noise", QUERY, summary="Paisagem")
    assert fuse_and_cosine([noise, caption], QUERY, "paywall")[0][0] == "caption"

    dashboard = _row("dashboard", WEAK)
    dashboard["analysis"] = {"summary": "Painel", "topics": ["dashboard"]}
    landscape = _row("landscape", QUERY, summary="Paisagem")
    ranked = fuse_and_cosine([landscape, dashboard], QUERY, "interface")[0]
    assert ranked[0] == "dashboard" and "landscape" in ranked


def test_checker_failure_returns_the_ranked_list_and_wrong_color_drops():
    rows = [_saved("keep", "Interface clara"), _saved("also", "Outra interface")]

    class Down:
        def verify(self, query, refs):
            raise AIError("provider_unavailable")

    ids, failed = apply_checker(Down(), "interface", rows)
    assert failed and ids == ["keep", "also"]

    class KeepAll:
        def verify(self, query, refs):
            return [ref.id for ref in refs]

    colored = [
        _saved("green", "Botão verde", colors=["verde / green"]),
        _saved("purple", "Landing roxa", colors=["roxo / purple"]),
    ]
    ids, failed = apply_checker(KeepAll(), "web design roxo", colored)
    assert not failed and ids == ["purple"]


def test_recall_file_rejects_content_fields(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text('{"labeled": [{"query": "login", "relevant": [], "title": "segredo"}],'
                    ' "qualitative": ["logos verdes"]}')
    with pytest.raises(ValueError):
        load_queries(path)
