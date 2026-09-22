import json
import subprocess
import wave
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from google.api_core.exceptions import ServiceUnavailable

from queep.accounts import Accounts
from queep.ai import AI
from queep.config import Settings, database, image_bucket
from queep.jobs import Jobs
from queep.media import MAX_BYTES, MediaEvidence, MediaFiles, evidence, prepare
from queep.models import Interpretation, Reference
from queep.repository import Repository
from queep.worker import process


def wav(seconds=2):
    target = BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * int(seconds * 16000))
    return target.getvalue()


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    audio = root / "audio.wav"
    audio.write_bytes(wav())
    output = root / "video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=5:d=2",
            "-i",
            str(audio),
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(output),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return output.read_bytes()


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_media_upload_retry_playback_isolation_and_deletion(client, account, video, kind):
    uid, headers = account
    service = MediaFiles(database(), image_bucket())
    data = wav() if kind == "audio" else video
    identifier = str(uuid4())
    path = f"/v1/references/{identifier}"
    first = client.put(path + "/media", content=data, headers=headers)
    assert first.status_code == 200, first.text
    ref = first.json()
    assert ref["kind"] == kind and ref["media"]["has_audio"]
    assert 1.9 < ref["media"]["duration_seconds"] < 2.2
    assert client.put(path + "/media", content=data, headers=headers).json() == ref
    assert (
        client.put(path + "/media?title=Changed", content=data, headers=headers).status_code == 409
    )
    assert (
        client.put(path, json={"kind": "note", "text": "Conflicting"}, headers=headers).status_code
        == 409
    )
    assert client.get(path + "/media", headers=headers).status_code == 200
    assert client.get(path + "/media", headers=headers).headers["cache-control"] == "no-store"
    assert client.get(path + "/media").status_code == 401
    assert client.get(path + "/media?thumbnail=true", headers=headers).status_code == (
        200 if kind == "video" else 404
    )
    stranger = httpx.post(
        "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:signUp?key=local",
        json={"returnSecureToken": True},
    ).json()
    assert (
        client.get(
            path + "/media", headers={"Authorization": "Bearer " + stranger["idToken"]}
        ).status_code
        == 404
    )
    assert client.delete(path + "?revision=1", headers=headers).status_code == 204
    assert client.get(path + "/media", headers=headers).status_code == 404
    assert client.put(path + "/media", content=data, headers=headers).status_code == 409
    prefix = service.prefix(uid, identifier, ref["media"]["sha256"])
    assert not list(image_bucket().list_blobs(prefix=prefix))


@pytest.mark.parametrize(
    "data,status", [(b"", 413), (b"invalid", 415), (b"x" * (MAX_BYTES + 1), 413)]
)
def test_bad_upload_has_no_reference_or_reservation(client, account, data, status):
    uid, headers = account
    identifier = str(uuid4())
    assert (
        client.put(f"/v1/references/{identifier}/media", content=data, headers=headers).status_code
        == status
    )
    assert client.get("/v1/references", headers=headers).json()["items"] == []
    assert not MediaFiles(database(), image_bucket()).manifest(uid, identifier).get().exists


def test_duration_limit_and_visual_sampling(video):
    with pytest.raises(HTTPException) as error:
        prepare(wav(301))
    assert error.value.status_code == 413
    prepared = prepare(video)
    samples = evidence(prepared.original, prepared.info)
    assert samples.audio and 1 <= len(samples.frames) <= 8
    assert samples.frames[0][1].startswith(b"\xff\xd8")
    audio = prepare(wav())
    assert not evidence(audio.original, audio.info).frames


def test_interrupted_upload_can_resume_once(client, account, monkeypatch):
    uid, headers = account
    identifier = str(uuid4())
    service = MediaFiles(database(), image_bucket())
    original = MediaFiles.put_object
    monkeypatch.setattr(
        MediaFiles, "put_object", lambda *_: (_ for _ in ()).throw(ServiceUnavailable("test"))
    )
    assert (
        client.put(f"/v1/references/{identifier}/media", content=wav(), headers=headers).status_code
        == 503
    )
    assert service.manifest(uid, identifier).get().to_dict()["state"] == "pending"
    monkeypatch.setattr(MediaFiles, "put_object", original)
    assert (
        client.put(f"/v1/references/{identifier}/media", content=wav(), headers=headers).status_code
        == 200
    )
    assert len(client.get("/v1/references", headers=headers).json()["items"]) == 1


def test_recovery_preserves_media_and_final_erasure_removes_it(client, account, video):
    uid, headers = account
    service = MediaFiles(database(), image_bucket())
    identifier = str(uuid4())
    ref = service.upload(uid, identifier, "fixture", video)
    accounts = Accounts(database(), image_bucket(), Settings())
    now = datetime.now(UTC)
    status = accounts.request(uid, now=now, operation_id=str(uuid4()))
    assert status["status"] == "scheduled"
    assert client.get(f"/v1/references/{identifier}/media", headers=headers).status_code == 401
    accounts.cancel(uid, status["version"], str(uuid4()), now=now + timedelta(seconds=1))
    assert service.read(uid, identifier)[0]
    accounts.request(uid, immediate=True, version=status["version"] + 1, operation_id=str(uuid4()))
    assert accounts.process(uid, now=now + timedelta(minutes=11))
    assert not list(
        image_bucket().list_blobs(prefix=service.prefix(uid, identifier, ref.media.sha256))
    )


def test_worker_publishes_transcript_once_and_ignores_duplicate_delivery(video):
    service = MediaFiles(database(), image_bucket())
    uid, identifier = uuid4().hex, str(uuid4())
    service.upload(uid, identifier, "fixture", video)
    jobs = Jobs(database())
    calls = []

    def interpret(reference, samples):
        assert samples.audio and samples.frames
        calls.append("interpret")
        return Interpretation(
            summary="Organização de jogos", coverage="partial"
        ), "Aurora organiza jogos."

    def embed(document):
        assert "Aurora organiza jogos." in document
        return [1.0] + [0.0] * 767

    engine = SimpleNamespace(interpret_media=interpret, embed=embed)
    task = jobs.payload(service.repo.job(uid, identifier).get())
    assert process(task, jobs, engine, service, media=service) == "ready"
    assert process(task, jobs, engine, service, media=service) == "ignored"
    assert calls == ["interpret"]
    assert service.repo.get(uid, identifier).transcript == "Aurora organiza jogos."
    assert service.repo.get(uid, identifier).status == "ready"


def test_vision_never_receives_spoken_descriptions(video):
    prepared = prepare(video)
    now = datetime.now(UTC)
    ref = Reference(
        id="test",
        kind="video",
        title="botões brancos",
        text="",
        url=None,
        status="queued",
        revision=1,
        created_at=now,
        updated_at=now,
        media=prepared.info,
    )
    received = []

    def request(req):
        body = json.loads(req.content)
        received.append(body)
        result = Interpretation(
            summary="Fundo azul", coverage="partial", appearance=["azul"]
        ).model_dump()
        if len(received) == 1:
            result = {"transcript": "botões brancos"}
        elif len(received) == 2:
            result.update(
                summary="A narração descreve uma interface branca.",
                appearance=["branco"],
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
                "usage": {"cost": 0.001},
            },
        )

    budget = SimpleNamespace(reserve=lambda: "test", settle=lambda *_: None)
    engine = AI(
        "test",
        budget,
        httpx.Client(base_url="https://example.test/", transport=httpx.MockTransport(request)),
    )
    analysis, transcript = engine.interpret_media(ref, MediaEvidence(b"audio", [(1, b"image")]))
    assert transcript == "botões brancos" and analysis.appearance == ["azul"]
    assert len(received) == 3
    assert "input_audio" not in json.dumps(received[2])
    assert "botões brancos" not in json.dumps(received[2], ensure_ascii=False)
    assert "image_url" not in json.dumps(received[0])


def test_media_title_and_notes_edit_keeps_file_and_requeues(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    first = client.put(path + "/media?title=Reuni%C3%A3o", content=wav(), headers=headers)
    assert first.status_code == 200, first.text
    edit = {"kind": "audio", "title": "Reunião de kickoff", "text": "Decisões: prazo e escopo",
            "revision": 1, "mutation_id": str(uuid4())}
    edited = client.patch(path, json=edit, headers=headers)
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["title"], body["text"], body["revision"], body["status"]) == (
        "Reunião de kickoff", "Decisões: prazo e escopo", 2, "queued")
    assert body["media"] == first.json()["media"]
    assert body["analysis"] is None
    wrong_kind = {**edit, "kind": "video", "revision": 2, "mutation_id": str(uuid4())}
    assert client.patch(path, json=wrong_kind, headers=headers).status_code == 409
    assert Repository(database()).job(uid, ref_id).get().to_dict()["revision"] == 2
    assert client.get(path + "/media", headers=headers).status_code == 200
    assert client.delete(path + "?revision=2", headers=headers).status_code == 204
