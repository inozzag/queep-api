import json

import httpx
import pytest

from evaluation.provider import BudgetExhausted, EvaluationError, Router, routing, source_parts
from evaluation.run import cost_summary, report_scores
from evaluation.scoring import calibrate, evaluate, rank
from evaluation.verify import candidate, select

MODEL = {"id": "google/gemini-embedding-2", "provider": "google-vertex/us",
         "prompt_per_million": .2, "completion_per_million": 0, "dimensions": 2}
INTERPRETATION = {"summary": "Exemplo", "topics": [], "entities": [], "usefulness": [],
                  "appearance": [], "coverage": "full", "limitations": []}


def router(tmp_path, handler, budget=.02):
    client = httpx.Client(base_url="https://openrouter.ai/api/v1/",
                          transport=httpx.MockTransport(handler))
    return Router(client, tmp_path / "calls.json", budget, .005)


def test_no_labels_or_queries_sent_and_no_external_image_files():
    parts = source_parts({"id": "secret-label", "text": "Some content",
                          "expected": ["secret-label"], "query": "desired result"})
    assert "secret-label" not in json.dumps(parts)
    assert "desired result" not in json.dumps(parts)
    with pytest.raises(EvaluationError):
        source_parts({"image": "../../private.png"})
    policy = routing(MODEL)
    assert policy["only"] == ["google-vertex/us"]
    assert policy["zdr"] and policy["require_parameters"]
    assert not policy["allow_fallbacks"] and policy["data_collection"] == "deny"


def test_timeout_reserved_and_never_retried(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("sensitive response", request=request)

    api = router(tmp_path, handler, budget=.005)
    with pytest.raises(EvaluationError, match="Falha de rede"):
        api.request("embeddings", {"model": "test"}, "first")
    with pytest.raises(BudgetExhausted):
        api.request("embeddings", {"model": "test"}, "second")
    assert len(requests) == 1
    assert api.reserved == .005
    assert "sensitive" not in (tmp_path / "calls.json").read_text()


@pytest.mark.parametrize("finish,output", [
    ("length", INTERPRETATION), ("stop", {"summary": "missing fields"}),
    ("stop", {**INTERPRETATION, "unexpected": "field"}),
])
def test_invalid_interpretation_keeps_billed_cost(tmp_path, finish, output):
    api = router(tmp_path, lambda request: httpx.Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(output)}}],
        "usage": {"cost": .0002},
    }))
    with pytest.raises(EvaluationError, match="Interpretação"):
        api.interpret(MODEL, {"id": "x", "text": "example"}, 768)
    assert api.calls[0]["cost_usd"] == .0002
    assert api.calls[0]["status"] == "invalid_interpretation"


def test_partial_link_cannot_claim_full_coverage(tmp_path):
    api = router(tmp_path, lambda request: httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(INTERPRETATION)}}], "usage": {"cost": .0001},
    }))
    with pytest.raises(EvaluationError):
        api.interpret(MODEL, {"id": "link", "source_coverage": "partial"}, 768)


@pytest.mark.parametrize("data,model", [
    ([{"index": 0, "embedding": [1, 2]}, {"index": 0, "embedding": [1, 2]}], MODEL["id"]),
    ([{"index": 0, "embedding": [1]}], MODEL["id"]),
    ([{"index": 0, "embedding": [0, 0]}], MODEL["id"]),
    ([{"index": 0, "embedding": [1, 2]}], "different-model"),
])
def test_incompatible_vectors_fail_closed(tmp_path, data, model):
    api = router(tmp_path, lambda request: httpx.Response(200, json={
        "model": model, "data": data, "usage": {"cost": .00001},
    }))
    with pytest.raises(EvaluationError, match="Embedding inválido"):
        api.embed(MODEL, ["one input"], "documents")


def test_embedding_indexes_sorted_and_unknown_cost_explicit(tmp_path):
    api = router(tmp_path, lambda request: httpx.Response(200, json={
        "model": MODEL["id"], "usage": {"prompt_tokens": 30}, "data": [
            {"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]},
        ],
    }))
    assert api.embed(MODEL, ["first", "second"], "docs") == [[1, 0], [0, 1]]
    assert cost_summary(api.calls)["calls_without_reported_cost"] == 1


def test_native_model_alias_must_be_explicitly_allowed(tmp_path):
    model = {**MODEL, "response_ids": [MODEL["id"], "gemini-embedding-2"]}
    api = router(tmp_path, lambda request: httpx.Response(200, json={
        "model": "gemini-embedding-2", "usage": {"cost": .00001},
        "data": [{"index": 0, "embedding": [1, 0]}],
    }))
    assert api.embed(model, ["source"], "docs") == [[1, 0]]


def test_cost_above_reservation_stops_further_sends(tmp_path):
    api = router(tmp_path, lambda request: httpx.Response(200, json={"usage": {"cost": .03}}))
    with pytest.raises(BudgetExhausted):
        api.request("embeddings", {"model": "test"}, "first")
    with pytest.raises(BudgetExhausted):
        api.request("embeddings", {"model": "test"}, "second")
    assert len(api.calls) == 1


def test_metrics_penalize_omissions_irrelevance_and_empty_false_positives():
    queries = [
        {"id": "k", "mode": "known", "expected": ["wanted"]},
        {"id": "s", "mode": "set", "expected": ["a", "missing"]},
        {"id": "e", "mode": "empty", "expected": []},
    ]
    rankings = {"k": [("other", .9), ("wanted", .8)],
                "s": [("a", .9), ("wrong", .8)], "e": [("wrong", .9)]}
    scores = evaluate(queries, rankings, .7)
    assert scores["known_hit_at_5"] == 1
    assert scores["set_precision"] == scores["set_recall"] == .5
    assert scores["empty_false_positive_rate"] == 1
    assert evaluate(queries, rankings, 1.01)["set_precision"] == 0
    filtered = evaluate(queries, rankings, 1.01)
    assert filtered["known_hit_at_5"] == 0
    assert filtered["raw_rank_hit_at_5"] == 1


def test_calibration_cannot_use_test_labels_and_missing_items_remain_failures():
    queries = [{"id": "c", "query": "calibrate", "split": "calibration", "mode": "known",
                "format": "text", "expected": ["wanted"]},
               {"id": "t", "query": "hold out", "split": "test", "mode": "known",
                "format": "text", "expected": ["failed-analysis"]}]
    with pytest.raises(ValueError, match="Calibration"):
        calibrate(queries, {q["id"]: [("wanted", .9)] for q in queries})
    scores = report_scores({"queries": queries}, {"wanted": [1, 0]}, [[1, 0], [0, 1]])
    assert scores["test"]["known_count"] == 1
    assert scores["test"]["known_hit_at_5"] == 0
    assert rank({"a": [1, 0], "b": [0, 1]}, [0, 1])[0][0] == "b"


def test_search_verification_uses_original_partial_source_without_speculation():
    ref = {"id": "link", "kind": "link", "title": "A course", "text": "",
           "source_coverage": "partial", "expected": ["should-not-leak"]}
    material = candidate(ref, {"summary": "Invented curriculum and pricing"})
    assert "Invented" not in json.dumps(material)
    assert "should-not-leak" not in json.dumps(material)
    assert material["title"] == "A course"


@pytest.mark.parametrize("ids", [["not-in-candidates"], ["a", "a"], "a"])
def test_search_verification_rejects_invented_or_duplicate_ids(tmp_path, ids):
    api = router(tmp_path, lambda request: httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"ids": ids})}}], "usage": {"cost": .0001},
    }))
    with pytest.raises(EvaluationError, match="Seleção inválida"):
        select(api, MODEL, "find something", [{"id": "a", "text": "some content"}])
    assert api.calls[0]["cost_usd"] == .0001


def test_search_verification_can_return_no_results(tmp_path):
    api = router(tmp_path, lambda request: httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ids":[]}'}}],
        "usage": {"cost": .0001},
    }))
    assert select(api, MODEL, "unrelated", [{"id": "a", "text": "some content"}]) == []
