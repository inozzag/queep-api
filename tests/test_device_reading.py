"""Readings made on Apple devices and the Vertex interpretation route."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from queep import device
from queep.ai import AI, INTERPRETER
from queep.config import database
from queep.media import MediaEvidence
from queep.models import DeviceReading, Interpretation, Reference
from queep.repository import Repository
from queep.search import search
from tests.test_ai_processing import fake_client
from tests.test_search_recall import QUERY, put_ready

NOW = datetime.now(UTC)
RESULT = Interpretation(summary="Tela de onboarding", coverage="full").model_dump()


def reference(kind="image"):
    return Reference(id="ref", kind=kind, title="", text="", url=None, status="queued",
                     revision=1, created_at=NOW, updated_at=NOW)


def answer(value):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            "usage": {"cost": .001}}


def test_interpreter_is_pinned_to_a_zdr_vertex_route():
    ai, calls, _ = fake_client(answer(RESULT))
    ai.interpret(reference("note").model_copy(update={"text": "Onboarding em três passos"}))
    assert calls[0]["model"] == INTERPRETER["id"] == "google/gemini-3.1-flash-lite"
    assert calls[0]["provider"]["only"] == ["google-vertex/global"]
    assert calls[0]["provider"]["zdr"] and not calls[0]["provider"]["allow_fallbacks"]


def test_device_ocr_and_colors_reach_the_interpreter():
    ai, calls, _ = fake_client(answer(RESULT))
    reading = DeviceReading(screen_text="Comece agora", colors=["#5B2EFF"], engine="apple-vision")
    ai.interpret(reference(), b"not-an-image", reading)
    sent = json.loads(calls[0]["messages"][1]["content"][0]["text"])
    assert sent["screen_text_ocr"] == "Comece agora"
    assert sent["pixel_colors_hex"] == ["#5B2EFF"]


def test_device_transcript_skips_paid_transcription():
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json=answer(RESULT))

    budget = SimpleNamespace(reserve=lambda: "ticket", settle=lambda *_: None)
    ai = AI("unused", budget, httpx.Client(base_url="https://test.invalid/",
                                           transport=httpx.MockTransport(respond)))
    analysis, transcript = ai.interpret_media(
        reference("audio"), MediaEvidence(b"audio", []), "Fala lida no iPhone.")
    assert transcript == "Fala lida no iPhone."
    assert analysis.summary.startswith("No áudio:")
    assert len(received) == 1
    assert "input_audio" not in json.dumps(received[0])


def test_reading_rejects_colors_that_are_not_hex():
    with pytest.raises(ValidationError):
        DeviceReading(colors=["roxo"])


def test_route_stores_a_reading_for_the_worker(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    body = {"screen_text": "Pricing", "transcript": "Olá", "colors": ["#FFFFFF"],
            "segments": [{"start": 0.0, "text": "Olá"}], "engine": "apple-speech"}
    response = client.put(f"/v1/references/{ref_id}/device-reading", headers=headers, json=body)
    assert response.status_code == 204, response.text
    stored = device.load(database(), uid, ref_id)
    assert stored.screen_text == "Pricing" and stored.segments[0].text == "Olá"
    assert device.load(database(), uid, str(uuid4())) is None


def test_search_without_server_check_returns_the_ranked_head():
    repo, uid = Repository(database()), uuid4().hex
    put_ready(repo, uid, "only", QUERY)

    def verify(*_):
        raise AssertionError("the device checks the head")

    ai = SimpleNamespace(budget=SimpleNamespace(rate_limit=lambda _: None),
                         embed=lambda *_args, **_kwargs: QUERY, verify=verify)
    result = search(repo, ai, uid, "interface de CRM", verify=False)
    assert [item.id for item in result.items] == ["only"]
