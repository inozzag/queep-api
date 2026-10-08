import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from queep.ai import (
    AI,
    CHECKER_BUDGET,
    CHECKER_TIMEOUT,
    SPACE,
    AIError,
    document,
    generated_title,
    headline_title,
)
from queep.budget import Budget
from queep.config import database
from queep.jobs import Jobs
from queep.main import app as public_app
from queep.models import Capture, Edit, ImageInfo, Interpretation, SourceContent
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


def test_testflight_outbox_cannot_be_dispatched_or_processed_by_development(monkeypatch):
    db = database()
    root = db.collection("outbox_isolation_tests").document(uuid4().hex)

    class ScopedDB:
        def collection(self, name):
            return root.collection(name)

        def transaction(self):
            return db.transaction()

    scoped = ScopedDB()
    uid, ref_id = uuid4().hex, str(uuid4())
    monkeypatch.setenv("QUEEP_JOB_COLLECTION", "jobs")
    development = Jobs(scoped)
    monkeypatch.setenv("QUEEP_JOB_COLLECTION", "jobs_testflight")
    testflight = Jobs(scoped)
    ref = testflight.repo.save(uid, ref_id, Capture(kind="note", text="TestFlight only"))
    task = task_for(testflight.repo, testflight, uid, ref)
    assert testflight.repo.job(uid, ref_id).id == development.repo.job(uid, ref_id).id
    assert development.due() == []
    assert development.claim(task) is None
    assert process(task, development, None, None) == "ignored"
    assert [item.id for item in testflight.due()] == [task["job_id"]]
    calls = []
    ai = SimpleNamespace(interpret=lambda *_: calls.append("interpret") or ANALYSIS,
                         embed=lambda *_: calls.append("embed") or VECTOR)
    assert process(task, testflight, ai, None) == "ready"
    assert calls == ["interpret", "embed"]
    assert testflight.repo.get(uid, ref_id).status == "ready"

    # Reverse direction: the sandbox dispatcher must also leave development jobs alone.
    dev_ref = development.repo.save(uid, str(uuid4()), Capture(kind="note", text="Dev only"))
    dev_task = task_for(development.repo, development, uid, dev_ref)
    assert testflight.due() == []
    assert process(dev_task, testflight, None, None) == "ignored"
    assert development.repo.job(uid, dev_ref.id).get().exists


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


def test_semantic_search_never_returns_or_spends_on_archived_references(library):
    repo, jobs, uid, ref = library
    publish(*library)
    repo.archive(uid, ref.id, True)
    result = search(repo, None, uid, "CRM arquivado")
    assert result.items == []
    assert not result.has_pending


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


def test_checker_timeout_releases_the_budget_placeholder(library):
    *_, ref = library
    seen = {}

    def post(_path, json, timeout):
        seen["timeout"] = timeout
        raise httpx.ReadTimeout("slow checker")

    settled = []
    budget = SimpleNamespace(reserve=lambda: "ticket", settle=lambda *args: settled.append(args))
    ai = AI("unused", budget, SimpleNamespace(post=post))
    with pytest.raises(AIError, match="provider_timeout"):
        ai.verify("interface", [ref])
    assert seen["timeout"] == CHECKER_TIMEOUT == 20
    assert settled[0][1] == 0.0


def test_provider_error_without_usage_keeps_the_placeholder():
    def post(_path, json, timeout):
        raise httpx.ConnectError("down")

    settled = []
    budget = SimpleNamespace(reserve=lambda: "ticket", settle=lambda *args: settled.append(args))
    ai = AI("unused", budget, SimpleNamespace(post=post))
    with pytest.raises(AIError, match="provider_unavailable"):
        ai.embed("interface")
    assert settled[0][1] is None


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
                         fetched_at=datetime.now(UTC), native_title=True,
                         limitations=["Somente texto acessível."])


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


def test_post_photo_is_stored_and_sent_to_interpreter(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(
        uid,
        str(uuid4()),
        Capture(kind="link", url="https://x.com/richardrx/status/2099853605054316775"),
    )
    photo = picture()
    seen = []

    class Photos:
        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("store", data))
            return ImageInfo(
                sha256="a" * 64,
                content_type="image/jpeg",
                width=900,
                height=600,
                byte_count=len(data),
            )

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    photos = Photos()
    monkeypatch.setattr("queep.worker.read_photo", lambda _url: photo)
    ai = SimpleNamespace(
        interpret=lambda _ref, data=None: seen.append(("interpret", data)) or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return source_fixture().model_copy(update={"content_kind": "metadata"}), "https://pbs.twimg.com/media/abc.jpg"
    assert process(task_for(repo, jobs, uid, ref), jobs, ai, photos, reader) == "ready"
    assert seen[0] == ("store", photo) and seen[1] == ("interpret", [photo])
    stored = repo.get(uid, ref.id)
    assert stored.kind == "link" and stored.image and stored.image.width == 900


def test_post_carousel_photos_are_stored_in_order(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(
        uid,
        str(uuid4()),
        Capture(kind="link", url="https://x.com/jenidesignns/status/2099804636328648823"),
    )
    first, second = picture("white"), picture("red")
    seen = []

    class Photos:
        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("store", data))
            return ImageInfo(
                sha256=("a" if data == first else "b") * 64,
                content_type="image/jpeg",
                width=900,
                height=600,
                byte_count=len(data),
            )

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    photos = Photos()
    monkeypatch.setattr(
        "queep.worker.read_photo",
        lambda url: first if url.endswith("one.jpg") else second,
    )
    ai = SimpleNamespace(
        interpret=lambda _ref, data=None: seen.append(("interpret", data)) or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return source_fixture().model_copy(update={"content_kind": "metadata"}), [
            "https://pbs.twimg.com/media/one.jpg",
            "https://pbs.twimg.com/media/two.jpg",
        ]
    assert process(task_for(repo, jobs, uid, ref), jobs, ai, photos, reader) == "ready"
    assert seen[0] == ("store", first) and seen[1] == ("store", second)
    assert seen[2] == ("interpret", [first, second])
    stored = repo.get(uid, ref.id)
    assert stored.image.sha256 == "a" * 64
    assert [item.sha256 for item in stored.images] == ["a" * 64, "b" * 64]


def test_post_video_poster_is_marked_and_sent_to_interpreter(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(
        uid,
        str(uuid4()),
        Capture(kind="link", url="https://x.com/a/status/2099804636328648823"),
    )
    poster = picture("blue")
    seen = []

    class Photos:
        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("store", preview_kind, data))
            return ImageInfo(
                sha256="v" * 64,
                content_type="image/jpeg",
                width=900,
                height=600,
                byte_count=len(data),
                preview_kind=preview_kind,
            )

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_photo", lambda _url: poster)
    ai = SimpleNamespace(
        interpret=lambda _ref, data=None: seen.append(("interpret", data)) or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return source_fixture().model_copy(update={"content_kind": "metadata"}), [{
            "url": "https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg",
            "kind": "video",
        }]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    assert seen[0][:2] == ("store", "video") and seen[1] == ("interpret", [poster])
    stored = repo.get(uid, ref.id)
    assert stored.images[0].preview_kind == "video"


def test_post_photos_are_interpreted_when_tweet_text_is_missing(library):
    from tests.test_images import picture

    repo, _, uid, _ = library
    fallback = SourceContent(
        content_kind="metadata", state="read", url="https://x.com/yurygok/status/1",
        title="Post", text="", fetched_at=datetime.now(UTC),
        limitations=[
            "Foi lido o conteúdo público do post; o texto completo pode estar indisponível.",
        ],
    )
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/yurygok/status/1"))
    ref = ref.model_copy(update={"source": fallback})
    analysis = Interpretation(summary="Mesa de mixer com faders coloridos", coverage="partial",
                              appearance=["mesa de som digital", "faders verde roxo e rosa"],
                              headline="Mesa mixer faders coloridos")
    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": analysis.model_dump_json()}}]})
    result = ai.interpret(ref, [picture()])
    payload = json.dumps(calls[0]["messages"][1]["content"])
    assert "image_url" in payload
    assert result.appearance == analysis.appearance
    assert "mesa de som digital" in document(ref, result)


def test_gallery_interpretation_ignores_extra_keys_and_clips_appearance(library):
    from tests.test_images import picture

    repo, _, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/a/status/1"))
    analysis = Interpretation(
        summary="Três telas de um produto", coverage="partial",
        appearance=["ok"], headline="Telas de um produto digital")
    payload = analysis.model_dump()
    payload["photos"] = ["https://example.test/1.jpg"]
    payload["appearance"] = ["x" * 600]
    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(payload)}}]})
    result = ai.interpret(ref, [picture("white"), picture("red")])
    assert calls[0]["max_tokens"] == 1536
    images = [
        part for part in calls[0]["messages"][1]["content"] if part.get("type") == "image_url"]
    assert len(images) == 2
    assert result.appearance == ["x" * 500]
    assert result.headline == "Telas de um produto digital"


def test_unreadable_gallery_frame_is_skipped(library):
    from tests.test_images import picture

    repo, _, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/a/status/1"))
    analysis = Interpretation(summary="Uma tela escura", coverage="partial")
    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": analysis.model_dump_json()}}]})
    result = ai.interpret(ref, [b"not-an-image", picture()])
    images = [
        part for part in calls[0]["messages"][1]["content"] if part.get("type") == "image_url"]
    assert len(images) == 1
    assert result.summary == "Uma tela escura"


def test_generated_title_uses_image_headline_not_url_or_post():
    visual = Interpretation(
        summary="https://x.com/yurygok/status/1",
        headline="Mesa mixer faders coloridos",
        coverage="partial",
        appearance=["mesa de som digital"],
    )
    assert generated_title(visual) == "Mesa mixer faders coloridos"
    assert headline_title("Post") == ""
    assert headline_title("https://x.com/yurygok/status/1") == ""
    untitled = Interpretation(summary="Post", coverage="partial",
                              appearance=["mesa de som digital", "faders verde"])
    assert generated_title(untitled) == "mesa de som digital; faders verde"


def test_library_title_keeps_native_network_title(library):
    from queep.worker import library_title

    repo, _, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://youtu.be/dQw4w9WgXcQ"))
    source = SourceContent(
        content_kind="metadata", state="read", url=ref.url or "", title="Official video",
        text="Título: Official video", fetched_at=datetime.now(UTC), native_title=True,
        limitations=["Somente título e autor públicos foram lidos."])
    ref = ref.model_copy(update={"source": source})
    analysis = Interpretation(summary="Um clipe famoso", headline="Clipe de rick astley",
                              coverage="partial")
    assert library_title(ref, analysis, []) == "Official video"


def test_library_title_skips_posts_without_text_or_media(library):
    from queep.worker import library_title

    repo, _, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/a/status/1"))
    source = SourceContent(
        content_kind="metadata", state="read", url=ref.url or "", title="Ada",
        text="", fetched_at=datetime.now(UTC), native_title=False, author_name="Ada",
        author_handle="ada", limitations=["Foi lido o autor público do post."])
    ref = ref.model_copy(update={"source": source})
    analysis = Interpretation(summary="Ada", headline="Post sem conteúdo", coverage="partial")
    assert library_title(ref, analysis, []) is None
    assert library_title(ref, analysis, [b"frame"]) == "Post sem conteúdo"


def test_library_title_uses_ai_when_network_has_no_native_title(library):
    from queep.worker import library_title

    repo, _, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/a/status/1"))
    source = SourceContent(
        content_kind="metadata", state="read", url=ref.url or "", title="Ada",
        text="Um mixer no estúdio com faders coloridos", fetched_at=datetime.now(UTC),
        native_title=False, author_name="Ada", author_handle="ada",
        limitations=["Foi lido o texto público do post."])
    ref = ref.model_copy(update={"source": source})
    analysis = Interpretation(summary="Mixer no estúdio", headline="Mixer colorido no estúdio",
                              coverage="partial")
    assert library_title(ref, analysis, []) == "Mixer colorido no estúdio"


def test_library_title_never_names_a_profile(library):
    from queep.worker import library_title

    repo, _, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/ada"))
    source = SourceContent(
        content_kind="profile", state="read", url=ref.url or "", title="Ada Lovelace",
        text="Notas sobre computacao.", fetched_at=datetime.now(UTC), native_title=True,
        author_name="Ada Lovelace", author_handle="ada")
    ref = ref.model_copy(update={"source": source})
    analysis = Interpretation(summary="Bio de ada", headline="Notas sobre computacao pura",
                              coverage="full")
    assert library_title(ref, analysis, []) is None


def test_library_title_strips_product_name_from_website_headline(library):
    from queep.worker import library_title

    repo, _, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://cursor.com"))
    source = SourceContent(
        content_kind="page", state="read", url=ref.url or "", title="Cursor",
        text="Editor de codigo com agentes para times.", fetched_at=datetime.now(UTC),
        native_title=False, limitations=["Somente texto acessível."])
    ref = ref.model_copy(update={"source": source})
    analysis = Interpretation(
        summary="Editor de codigo com agentes",
        headline="Cursor: Editor de codigo com agentes",
        coverage="partial")
    assert library_title(ref, analysis, []) == "Editor de codigo com agentes"


def test_process_stores_profile_avatar_without_gallery_or_title(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/ada"))
    avatar = picture("green")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append("author")
            return ImageInfo(sha256="b" * 64, content_type="image/jpeg",
                             width=400, height=400, byte_count=len(data))

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append("gallery")
            return ImageInfo(sha256="a" * 64, content_type="image/jpeg",
                             width=900, height=600, byte_count=len(data))

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_photo", lambda url: avatar)
    ai = SimpleNamespace(
        interpret=lambda ref, data=None: seen.append(("interpret", data, ref.source.text))
        or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="profile", state="read", url="https://x.com/ada",
            title="Ada", text="Bio curta.", fetched_at=datetime.now(UTC),
            native_title=True, author_name="Ada", author_handle="ada",
        ), [
            {"url": "https://pbs.twimg.com/profile_images/ada.jpg", "kind": "avatar"},
            {"url": "https://pbs.twimg.com/media/one.jpg", "kind": "photo"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert seen == ["author", ("interpret", None, "Bio curta.")]
    assert stored.author_image.sha256 == "b" * 64
    assert stored.images is None
    assert stored.title == ""
    assert stored.source.content_kind == "profile"


def test_process_stores_dribbble_avatar_outside_profile_gallery(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://dribbble.com/ada"))
    avatar = picture("green")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append(("author", data))
            return ImageInfo(
                sha256="d" * 64,
                content_type="image/jpeg",
                width=400,
                height=400,
                byte_count=len(data),
            )

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("gallery", data))
            return ImageInfo(
                sha256="e" * 64,
                content_type="image/jpeg",
                width=900,
                height=600,
                byte_count=len(data),
            )

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_public_image", lambda url: avatar)
    ai = SimpleNamespace(
        interpret=lambda ref, data=None: seen.append(("interpret", data, ref.source.text))
        or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="profile",
            state="read",
            url="https://dribbble.com/ada",
            title="Ada Lovelace",
            text="Design de produtos digitais.",
            fetched_at=datetime.now(UTC),
            native_title=True,
            author_name="Ada Lovelace",
            author_handle="ada",
        ), [
            {"url": "https://cdn.dribbble.com/avatar.png", "kind": "avatar"},
            {"url": "https://cdn.dribbble.com/shot.png", "kind": "photo"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert seen == [
        ("author", avatar),
        ("interpret", None, "Design de produtos digitais."),
    ]
    assert stored.author_image.sha256 == "d" * 64
    assert stored.images is None and stored.title == ""
    assert stored.source.content_kind == "profile"


def test_process_skips_profile_interpretation_without_bio(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/ada"))
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            return ImageInfo(sha256="b" * 64, content_type="image/jpeg",
                             width=400, height=400, byte_count=len(data))

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_photo", lambda url: picture())
    ai = SimpleNamespace(
        interpret=lambda *_: seen.append("interpret") or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="profile", state="read", url="https://x.com/ada",
            title="Ada", text="", fetched_at=datetime.now(UTC),
            native_title=True, author_name="Ada", author_handle="ada",
        ), [{"url": "https://pbs.twimg.com/profile_images/ada.jpg", "kind": "avatar"}]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert "interpret" not in seen
    assert stored.analysis.topics == []
    assert stored.analysis.limitations == ["O perfil não tem bio pública para analisar."]
    assert stored.title == ""


def test_post_visuals_reach_embedding_and_verification(library, monkeypatch):
    from queep.sources import tweet_photo_fallback
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(
        kind="link", url="https://x.com/yurygok/status/2100117746117673043"))
    analysis = Interpretation(summary="Mesa de mixer com faders coloridos", coverage="partial",
                              appearance=["mesa de som digital", "faders verde roxo e rosa"],
                              headline="Mesa mixer faders coloridos")
    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": analysis.model_dump_json()}}]})
    documents = []
    ai.embed = lambda text: documents.append(text) or VECTOR
    photo = picture()

    class Photos:
        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            return ImageInfo(sha256="a" * 64, content_type="image/jpeg",
                             width=900, height=600, byte_count=len(data))

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_photo", lambda _url: photo)

    def reader(_url):
        url = "https://x.com/yurygok/status/2100117746117673043"
        return tweet_photo_fallback(url, ["https://pbs.twimg.com/media/mixer.jpg"]), [
            "https://pbs.twimg.com/media/mixer.jpg",
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    assert "image_url" in json.dumps(calls[0]["messages"][1]["content"])
    assert "mesa de som digital" in documents[0]
    stored = repo.get(uid, ref.id)
    assert stored.title == "Mesa mixer faders coloridos"
    verifier, requests, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"requested_kind": "any", "ids": [stored.id]})}}]})
    assert verifier.verify("mesa de mixer com faders", [stored]) == [stored.id]
    candidate = json.loads(requests[0]["messages"][1]["content"])["candidates"][0]
    assert candidate["image_description"]["appearance"] == ["mesa de som digital",
                                                            "faders verde roxo e rosa"]


def test_process_stores_author_avatar_outside_gallery(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/ada/status/1"))
    photo = picture()
    avatar = picture("green")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append("author")
            return ImageInfo(sha256="b" * 64, content_type="image/jpeg",
                             width=400, height=400, byte_count=len(data))

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append("gallery")
            return ImageInfo(sha256="a" * 64, content_type="image/jpeg",
                             width=900, height=600, byte_count=len(data))

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_photo",
                        lambda url: avatar if "profile" in url else photo)
    ai = SimpleNamespace(interpret=lambda _ref, data=None: ANALYSIS, embed=lambda _text: VECTOR)

    def reader(_url):
        return source_fixture().model_copy(update={
            "content_kind": "metadata", "native_title": False, "title": "Ada",
        }), [
            {"url": "https://pbs.twimg.com/profile_images/ada.jpg", "kind": "avatar"},
            {"url": "https://pbs.twimg.com/media/one.jpg", "kind": "photo"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert seen == ["author", "gallery"]
    assert stored.author_image.sha256 == "b" * 64
    assert stored.images[0].sha256 == "a" * 64


def test_first_video_clip_speech_reaches_interpreter(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://x.com/a/status/1"))
    poster = picture("blue")
    seen = []

    class Photos:
        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            return ImageInfo(sha256="v" * 64, content_type="image/jpeg",
                             width=900, height=600, byte_count=len(data),
                             preview_kind=preview_kind)

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_photo", lambda _url: poster)
    monkeypatch.setattr(
        "queep.worker.read_social_clip", lambda url: seen.append(("clip", url)) or b"mp4")
    monkeypatch.setattr("queep.worker.social_evidence", lambda data: SimpleNamespace(
        audio=b"mp3", frames=[(0.2, poster)]))
    ai = SimpleNamespace(
        transcribe_speech=lambda audio: seen.append(("speech", audio)) or "fala do autor",
        interpret=lambda ref, data=None: seen.append(("interpret", ref.transcript)) or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return source_fixture().model_copy(update={
            "content_kind": "metadata", "native_title": False, "title": "Ada",
        }), [{
            "url": "https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg",
            "kind": "video",
            "clip_url": "https://video.twimg.com/amplify_video/clip.mp4",
        }]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    assert ("clip", "https://video.twimg.com/amplify_video/clip.mp4") in seen
    assert ("speech", b"mp3") in seen
    assert ("interpret", "fala do autor") in seen
    assert repo.get(uid, ref.id).transcript == "fala do autor"


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
    assert len(calls[0]["messages"][1]["content"].encode()) <= CHECKER_BUDGET
    assert calls[0]["max_tokens"] == 160
    assert len(calls[0]["messages"][0]["content"]) < 1200
    assert len(document(refs[0], ANALYSIS).encode()) < 7000


def test_embedded_text_and_checker_keep_v8_fields(library):
    *_, ref = library
    analysis = Interpretation(
        summary="Landing com hero roxo", coverage="partial", headline="Hero roxo de produto",
        appearance=["fundo roxo"], ui_types=["landing page", "hero"],
        colors=["roxo / purple"], brand="Queep", on_screen_text=["Start free"],
        hook="Uma abertura curta", style="flat")
    source = source_fixture().model_copy(update={"text": "x" * 4000})
    current = ref.model_copy(update={"analysis": analysis, "source": source, "text": "nota"})
    embedded = document(current, analysis)
    assert "roxo / purple" in embedded and "landing page" in embedded
    assert "Start free" in embedded and "fundo roxo" in embedded
    assert embedded.index("Cores") < embedded.index("Página")
    assert len(embedded.encode()) <= 6800
    ai, calls, _ = fake_client({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"requested_kind": "any", "ids": [current.id]})}}]})
    assert ai.verify("roxo", [current]) == [current.id]
    candidate = json.loads(calls[0]["messages"][1]["content"])["candidates"][0]
    assert candidate["colors"] == ["roxo / purple"]
    assert candidate["ui_types"] == ["landing page", "hero"]
    assert candidate["on_screen_text"] == ["Start free"]
    assert candidate["brand"] == "Queep"


def test_process_stores_site_preview_without_vision_frames(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://cursor.com"))
    favicon = picture("green")
    preview = picture("blue")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append("author")
            return ImageInfo(sha256="b" * 64, content_type="image/jpeg",
                             width=400, height=400, byte_count=len(data))

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append("gallery")
            return ImageInfo(sha256="a" * 64, content_type="image/jpeg",
                             width=900, height=600, byte_count=len(data))

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_public_image",
                        lambda url: favicon if "favicon" in url else preview)
    ai = SimpleNamespace(
        interpret=lambda _ref, data=None: seen.append(("interpret", data)) or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="page", state="read", url="https://cursor.com", title="Cursor",
            text="Editor de codigo com agentes para times de produto.",
            fetched_at=datetime.now(UTC), native_title=False,
            limitations=["Somente texto acessível."],
        ), [
            {"url": "https://cursor.com/og.png", "kind": "photo"},
            {"url": "https://cursor.com/favicon.png", "kind": "avatar"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert seen[:2] == ["author", "gallery"]
    assert seen[2] == ("interpret", [preview])
    assert stored.author_image.sha256 == "b" * 64
    assert stored.images[0].sha256 == "a" * 64
    assert stored.source.title == "Cursor"


def test_process_stores_instagram_carousel_and_reel_clip(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://www.instagram.com/p/mix1/"))
    photo = picture()
    poster = picture("blue")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append("author")
            return ImageInfo(sha256="b" * 64, content_type="image/jpeg",
                             width=400, height=400, byte_count=len(data))

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("gallery", preview_kind))
            return ImageInfo(sha256="a" * 64, content_type="image/jpeg",
                             width=900, height=600, byte_count=len(data),
                             preview_kind=preview_kind)

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_public_image",
                        lambda url: poster if "poster" in url or "b.jpg" in url else photo)
    monkeypatch.setattr(
        "queep.worker.read_public_clip",
        lambda url: seen.append(("clip", url)) or b"mp4",
    )
    monkeypatch.setattr("queep.worker.social_evidence", lambda data: SimpleNamespace(
        audio=b"mp3", frames=[(0.2, poster)]))
    ai = SimpleNamespace(
        transcribe_speech=lambda audio: seen.append(("speech", audio)) or "fala do reel",
        interpret=lambda ref, data=None: seen.append(("interpret", ref.transcript)) or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="metadata", state="read", url="https://www.instagram.com/p/mix1/",
            title="Ada", text="Foto e um clip.", fetched_at=datetime.now(UTC),
            native_title=False, author_name="Ada", author_handle="ada",
            limitations=["Foi lido o texto público e até 4 mídias."],
        ), [
            {"url": "https://scontent.cdninstagram.com/avatar.jpg", "kind": "avatar"},
            {"url": "https://scontent.cdninstagram.com/a.jpg", "kind": "photo"},
            {"url": "https://scontent.cdninstagram.com/poster.jpg", "kind": "video",
             "clip_url": "https://scontent.cdninstagram.com/reel.mp4"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert ("clip", "https://scontent.cdninstagram.com/reel.mp4") in seen
    assert ("speech", b"mp3") in seen
    assert ("interpret", "fala do reel") in seen
    assert ("gallery", "photo") in seen and ("gallery", "video") in seen
    assert stored.author_image.sha256 == "b" * 64
    assert stored.transcript == "fala do reel"


def test_process_stores_behance_gallery_and_clip(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(
        uid, str(uuid4()), Capture(kind="link", url="https://www.behance.net/gallery/123/Mixer")
    )
    photo = picture()
    poster = picture("blue")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append("author")
            return ImageInfo(sha256="b" * 64, content_type="image/jpeg",
                             width=400, height=400, byte_count=len(data))

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("gallery", preview_kind))
            return ImageInfo(sha256="a" * 64, content_type="image/jpeg",
                             width=900, height=600, byte_count=len(data),
                             preview_kind=preview_kind)

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_public_image",
                        lambda url: poster if "poster" in url else photo)
    monkeypatch.setattr(
        "queep.worker.read_public_clip",
        lambda url: seen.append(("clip", url)) or b"mp4",
    )
    monkeypatch.setattr("queep.worker.social_evidence", lambda data: SimpleNamespace(
        audio=b"mp3", frames=[(0.2, poster)]))
    ai = SimpleNamespace(
        transcribe_speech=lambda audio: seen.append(("speech", audio)) or "fala do projeto",
        interpret=lambda ref, data=None: seen.append(("interpret", ref.transcript)) or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="metadata", state="read",
            url="https://www.behance.net/gallery/123/Mixer",
            title="Ada", text="Foto e um clip.", fetched_at=datetime.now(UTC),
            native_title=False, author_name="Ada", author_handle="ada",
            limitations=["Foi lido o texto público e até 4 mídias."],
        ), [
            {"url": "https://pps.services.adobe.com/avatar.jpg", "kind": "avatar"},
            {"url": "https://mir-s3-cdn-cf.behance.net/project_modules/1400/one.png",
             "kind": "photo"},
            {"url": "https://mir-s3-cdn-cf.behance.net/project_modules/1400/poster.png",
             "kind": "video", "clip_url": "https://cdn-prod-ccv.adobe.com/clip.mp4"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert ("clip", "https://cdn-prod-ccv.adobe.com/clip.mp4") in seen
    assert ("speech", b"mp3") in seen
    assert ("interpret", "fala do projeto") in seen
    assert ("gallery", "photo") in seen and ("gallery", "video") in seen
    assert stored.author_image.sha256 == "b" * 64
    assert stored.transcript == "fala do projeto"


def test_process_stores_behance_avatar_outside_profile_gallery(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://www.behance.net/ada"))
    avatar = picture("green")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append(("author", data))
            return ImageInfo(
                sha256="d" * 64,
                content_type="image/jpeg",
                width=400,
                height=400,
                byte_count=len(data),
            )

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("gallery", data))
            return ImageInfo(
                sha256="e" * 64,
                content_type="image/jpeg",
                width=900,
                height=600,
                byte_count=len(data),
            )

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem não encontrada")

    monkeypatch.setattr("queep.worker.read_public_image", lambda url: avatar)
    ai = SimpleNamespace(
        interpret=lambda ref, data=None: seen.append(("interpret", data, ref.source.text))
        or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="profile",
            state="read",
            url="https://www.behance.net/ada",
            title="Ada Lovelace",
            text="Design de produtos digitais.",
            fetched_at=datetime.now(UTC),
            native_title=True,
            author_name="Ada Lovelace",
            author_handle="ada",
        ), [
            {"url": "https://pps.services.adobe.com/avatar.jpg", "kind": "avatar"},
            {"url": "https://mir-s3-cdn-cf.behance.net/projects/404/cover.png", "kind": "photo"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert seen == [
        ("author", avatar),
        ("interpret", None, "Design de produtos digitais."),
    ]
    assert stored.author_image.sha256 == "d" * 64
    assert stored.images is None and stored.title == ""
    assert stored.source.content_kind == "profile"


def test_process_stores_pinterest_avatar_outside_profile_gallery(library, monkeypatch):
    from tests.test_images import picture

    repo, jobs, uid, _ = library
    ref = repo.save(
        uid,
        str(uuid4()),
        Capture(kind="link", url="https://www.pinterest.com/ada_design/"),
    )
    avatar = picture("green")
    seen = []

    class Photos:
        def attach_author(self, uid, ref_id, data):
            seen.append(("author", data))
            return ImageInfo(
                sha256="f" * 64,
                content_type="image/jpeg",
                width=400,
                height=400,
                byte_count=len(data),
            )

        def attach_preview(self, uid, ref_id, data, preview_kind="photo"):
            seen.append(("gallery", data))
            return ImageInfo(
                sha256="a" * 64,
                content_type="image/jpeg",
                width=900,
                height=600,
                byte_count=len(data),
            )

        def read(self, uid, ref_id, thumbnail=False):
            raise HTTPException(404, "Imagem nao encontrada")

    monkeypatch.setattr("queep.worker.read_public_image", lambda url: avatar)
    ai = SimpleNamespace(
        interpret=lambda ref, data=None: seen.append(("interpret", data, ref.source.text))
        or ANALYSIS,
        embed=lambda _text: VECTOR,
    )

    def reader(_url):
        return SourceContent(
            content_kind="profile",
            state="read",
            url="https://www.pinterest.com/ada_design/",
            title="Ada Lovelace",
            text="Design de produtos digitais.",
            fetched_at=datetime.now(UTC),
            native_title=True,
            author_name="Ada Lovelace",
            author_handle="ada_design",
        ), [
            {"url": "https://i.pinimg.com/280x280_RS/avatar.jpg", "kind": "avatar"},
            {"url": "https://i.pinimg.com/736x/pin.jpg", "kind": "photo"},
        ]

    assert process(task_for(repo, jobs, uid, ref), jobs, ai, Photos(), reader) == "ready"
    stored = repo.get(uid, ref.id)
    assert seen == [
        ("author", avatar),
        ("interpret", None, "Design de produtos digitais."),
    ]
    assert stored.author_image.sha256 == "f" * 64
    assert stored.images is None and stored.title == ""
    assert stored.source.content_kind == "profile"
