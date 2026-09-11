import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from queep.ai import AI, SPACE, AIError
from queep.budget import Budget
from queep.config import database
from queep.jobs import Jobs
from queep.main import app as public_app
from queep.models import Capture, Edit, Interpretation
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
        "content": json.dumps({"ids": [str(uuid4())]})}}]})
    with pytest.raises(AIError, match="invalid_selection"):
        ai.verify("CRM", [ref])
