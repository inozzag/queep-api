import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from queep.ai import AI, SPACE, AIError, document
from queep.budget import Budget
from queep.config import database
from queep.jobs import Jobs
from queep.main import app as public_app
from queep.models import Capture, Edit, Interpretation, SourceContent
from queep.repository import Repository
from queep.search import search
from queep.worker import app as worker_app
from queep.worker import process

VECTOR = [1.0] + [0.0] * 767
ANALYSIS = Interpretation(summary="Interface de CRM branca", coverage="full")


@pytest.fixture
def library():
    repo = Repository(database())
    uid, identifier = uuid4().hex, str(uuid4())
    ref = repo.save(uid, identifier, Capture(kind="note", text="Interface de CRM branca"))
    jobs = Jobs(repo.db)
    return repo, jobs, uid, ref


def task_for(repo, jobs, uid, ref):
    return jobs.payload(repo.job(uid, ref.id).get())


def publish(repo, jobs, uid, ref):
    work = jobs.claim(task_for(repo, jobs, uid, ref))
    assert jobs.finish(work, ANALYSIS, VECTOR)


def test_duplicate_delivery_does_not_repeat_ai(library):
    repo, jobs, uid, ref = library
    task = task_for(*library)
    calls = []
    ai = SimpleNamespace(interpret=lambda *_: calls.append("interpret") or ANALYSIS,
                         embed=lambda *_: calls.append("embed") or VECTOR)
    assert process(task, jobs, ai, None) == "ready"
    assert process(task, jobs, ai, None) == "ignored"
    assert calls == ["interpret", "embed"]
    record = repo.collection(uid).document(ref.id).get().to_dict()
    assert record["status"] == "ready" and record["embedding_space"] == SPACE


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_late_worker_never_publishes_after_mutation(library, action):
    repo, jobs, uid, ref = library
    work = jobs.claim(task_for(*library))
    if action == "edit":
        repo.save(uid, ref.id, Edit(kind="note", text="Tênis de corrida", revision=1,
                                    mutation_id=uuid4()), editing=True)
    else:
        repo.delete(uid, ref.id, 1)
    assert not jobs.current(work)
    assert not jobs.finish(work, ANALYSIS, VECTOR)
    record = repo.collection(uid).document(ref.id).get().to_dict()
    assert "embedding" not in record
    if action == "edit":
        assert record["text"] == "Tênis de corrida"
        assert repo.job(uid, ref.id).get().to_dict()["revision"] == 2
    else:
        assert record["deleted"] and not repo.job(uid, ref.id).get().exists


def test_crashed_lease_cannot_publish_and_retries_are_bounded(library):
    repo, jobs, uid, ref = library
    old = jobs.claim(task_for(*library))
    doc = repo.job(uid, ref.id)
    for attempt in range(1, 4):
        doc.update({"lease_until": datetime.now(UTC) - timedelta(seconds=1)})
        assert not jobs.finish(old, ANALYSIS, VECTOR)
        next_work = jobs.claim(task_for(*library))
        if attempt < 3:
            assert next_work["attempts"] == attempt + 1
        else:
            assert next_work is None
    assert repo.get(uid, ref.id).status == "error"
    run_id = doc.get().to_dict()["run_id"]
    assert jobs.retry(uid, ref.id, 1).status == "queued"
    assert doc.get().to_dict()["run_id"] != run_id
    assert jobs.claim(task_for(*library))["attempts"] == 1


def test_transient_failure_preserves_reference_and_retries(library):
    repo, jobs, uid, ref = library
    def fail(*_):
        raise AIError("provider_http_429")
    ai = SimpleNamespace(interpret=fail)
    for _ in range(3):
        assert process(task_for(*library), jobs, ai, None) == "retry_scheduled"
    assert repo.get(uid, ref.id).status == "error"
    assert repo.get(uid, ref.id).text == ref.text


def test_semantic_search_isolated_and_rechecks_deletions(library):
    repo, jobs, uid, ref = library
    publish(*library)
    other_uid = uuid4().hex
    other = repo.save(other_uid, str(uuid4()), Capture(kind="note", text="CRM privado"))
    publish(repo, jobs, other_uid, other)
    seen = []
    def verify(query, refs):
        seen.extend(item.id for item in refs)
        repo.delete(uid, ref.id, ref.revision)
        return [item.id for item in refs]
    ai = SimpleNamespace(budget=SimpleNamespace(rate_limit=lambda _: None),
                         embed=lambda *_, **__: VECTOR, verify=verify)
    result = search(repo, ai, uid, "Sistema comercial branco")
    assert seen == [ref.id]
    assert result.items == []


def test_empty_library_avoids_paid_search(library):
    repo, jobs, uid, ref = library
    result = search(repo, None, uid, "Uma coisa nova")
    assert result.items == [] and result.has_pending


def test_search_rejects_other_embedding_spaces(library):
    repo, jobs, uid, ref = library
    publish(*library)
    repo.collection(uid).document(ref.id).update({"embedding_space": "another-model"})
    assert search(repo, None, uid, "CRM").items == []


def test_budget_unknown_cost_and_duplicate_settlement(library):
    repo, *_ = library
    # Isolate the global budget namespace from other tests via a named emulator database path.
    class BudgetDB:
        def collection(self, name):
            return repo.db.collection("budget_tests").document(identifier).collection(name)
        def transaction(self):
            return repo.db.transaction()
    identifier = uuid4().hex
    budget = Budget(BudgetDB(), .021)
    first, second = budget.reserve(), budget.reserve()
    with pytest.raises(HTTPException) as blocked:
        budget.reserve()
    assert blocked.value.status_code == 429
    budget.settle(first, .001, {})
    budget.settle(first, 0, {})
    budget.settle(second, None, {})
    assert first.get().to_dict()["amount"] == .001
    assert second.get().to_dict()["amount"] == .01
    budget.reserve()
    with pytest.raises(HTTPException):
        budget.reserve()


def test_worker_routes_are_private_and_not_in_public_api():
    with TestClient(worker_app) as client:
        assert client.post("/tasks/dispatch").status_code == 401
        assert client.post("/tasks/process", json={}).status_code == 401
    assert not any(path.startswith("/tasks") for path in public_app.openapi()["paths"])


def fake_client(value):
    calls, settled = [], []
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=value)
    client = httpx.Client(base_url="https://test.invalid/", transport=httpx.MockTransport(respond))
    budget = SimpleNamespace(reserve=lambda: "ticket", settle=lambda *args: settled.append(args))
    return AI("unused", budget, client), calls, settled


@pytest.mark.parametrize("model,vector", [("wrong", VECTOR), ("gemini-embedding-2", [1.0]),
                                         ("gemini-embedding-2", [0.0] * 768)])
def test_embedding_rejects_incompatible_vectors(model, vector):
    ai, calls, settled = fake_client({"model": model, "data": [{"index": 0, "embedding": vector}],
                                     "usage": {"cost": .0001}})
    with pytest.raises(AIError, match="invalid_embedding"):
        ai.embed("CRM")
    assert isinstance(calls[0]["input"], str)
    assert calls[0]["provider"]["zdr"] and not calls[0]["provider"]["allow_fallbacks"]
    assert settled[0][1] == .0001


def test_partial_link_does_not_invent_or_fetch_site(library):
    repo, jobs, uid, ref = library
    link = repo.save(uid, str(uuid4()), Capture(kind="link", title="Ferramenta X",
                                                url="https://example.com"))
    ai, calls, _ = fake_client({})
    result = ai.interpret(link)
    assert result.coverage == "partial" and result.summary == "Ferramenta X"
    assert result.limitations and not result.usefulness and not calls


def test_search_rejects_ids_outside_candidates(library):
    *_, ref = library
    ai, _, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"requested_kind": "any", "ids": [str(uuid4())]})}}]})
    with pytest.raises(AIError, match="invalid_selection"):
        ai.verify("CRM", [ref])


def test_explicit_media_kind_rejects_inconsistent_model_selection(library):
    *_, ref = library
    audio = ref.model_copy(update={"id": "audio", "kind": "audio"})
    video = ref.model_copy(update={"id": "video", "kind": "video"})
    ai, _, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"requested_kind": "video", "ids": ["audio", "video"]})}}]})
    assert ai.verify("Vídeos sobre esta ferramenta", [audio, video]) == ["video"]


def source_fixture():
    return SourceContent(state="read", title="HTTPX", url="https://github.com/encode/httpx",
                         text="HTTPX is a Python HTTP client with synchronous and async APIs.",
                         fetched_at=datetime.now(UTC), limitations=["Somente texto acessível."])


def test_link_content_reaches_ai_embedding_verification_and_persistence(library):
    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://github.com/encode/httpx"))
    analysis = Interpretation(summary="Cliente HTTP Python síncrono e assíncrono", coverage="full")
    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": analysis.model_dump_json()}}]})
    documents = []
    ai.embed = lambda text: documents.append(text) or VECTOR
    reader_calls = []
    def reader(url):
        reader_calls.append(url)
        return source_fixture()
    task = task_for(repo, jobs, uid, ref)
    assert process(task, jobs, ai, None, reader) == "ready"
    assert process(task, jobs, ai, None, reader) == "ignored"
    current = repo.get(uid, ref.id)
    assert current.source == source_fixture().model_copy(update={
        "fetched_at": current.source.fetched_at})
    assert current.analysis.coverage == "partial" and len(reader_calls) == 1
    assert "synchronous and async" in calls[0]["messages"][1]["content"][0]["text"]
    assert "synchronous and async" in documents[0]
    verifier, requests, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"requested_kind": "any", "ids": [current.id]})}}]})
    assert verifier.verify("Cliente HTTP Python assíncrono", [current]) == [current.id]
    assert "synchronous and async" in requests[0]["messages"][1]["content"]
    assert json.loads(requests[0]["messages"][1]["content"])["candidates"][0]["kind"] == "link"
    # Editing discards old content, analysis and vector together.
    updated = repo.save(uid, ref.id, Edit(kind="link", url="https://example.com", revision=1,
                                         mutation_id=uuid4()), editing=True)
    assert updated.source is None and updated.analysis is None
    assert "embedding" not in repo.collection(uid).document(ref.id).get().to_dict()


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_source_fetch_in_flight_cannot_publish_after_mutation(library, action):
    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://example.com"))
    def reader(_):
        if action == "delete":
            repo.delete(uid, ref.id, 1)
        else:
            repo.save(uid, ref.id, Edit(kind="link", url="https://example.org", revision=1,
                                       mutation_id=uuid4()), editing=True)
        return source_fixture()
    assert process(task_for(repo, jobs, uid, ref), jobs, None, None, reader) == "stale"
    assert not repo.collection(uid).document(ref.id).get().to_dict().get("source")


def test_unavailable_source_still_becomes_searchable_from_user_context(library):
    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", text="Minha referência de tipografia",
                                               url="https://example.com"))
    ai, calls, _ = fake_client({})
    documents = []
    ai.embed = lambda text: documents.append(text) or VECTOR
    blocked = source_fixture().model_copy(update={"state": "unavailable", "text": ""})
    assert process(task_for(repo, jobs, uid, ref), jobs, ai, None, lambda _: blocked) == "ready"
    assert not calls and "tipografia" in documents[0]
    assert repo.get(uid, ref.id).source.state == "unavailable"


@pytest.mark.parametrize("kind", ["link", "image"])
def test_ten_long_sources_fit_verifier_input_budget(library, kind):
    *_, ref = library
    source = source_fixture().model_copy(update={"text": '\\"' * 6000, "title": "X" * 200})
    analysis = Interpretation(summary='\\"' * 1000, coverage="partial",
                              appearance=['\\"' * 40] * 6, entities=['\\"' * 30] * 6)
    refs = [ref.model_copy(update={"id": str(uuid4()), "kind": kind, "source": source,
                                 "title": "X" * 200, "text": '\\"' * 10000,
                                 "url": "https://example.com/" + "x" * 2000,
                                 "analysis": analysis}) for _ in range(10)]
    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": '{"requested_kind":"any","ids":[]}'}}]})
    assert ai.verify("Encontre uma referência", refs) == []
    assert len(calls[0]["messages"][1]["content"].encode()) <= 19500
    assert len(document(refs[0], ANALYSIS).encode()) < 7000
