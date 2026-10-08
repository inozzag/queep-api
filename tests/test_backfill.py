"""Backfill planning and writes, with a fake model and the Firestore emulator."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from queep.ai import ANALYSIS_VERSION, SPACE, AIError
from queep.backfill import (
    EMBED_BUILD,
    PROVIDER_REANALYZE_USD,
    PROVIDER_REEMBED_USD,
    PROVIDER_VIDEO_USD,
    Backfill,
    main,
    parse_args,
    provider_usd,
)
from queep.config import database
from queep.models import Capture, Interpretation
from queep.repository import Repository

VECTOR = [0.4] + [0.0] * 767


class FakeAI:
    def __init__(self, fail_first=False):
        self.interpreted = []
        self.embedded = []
        self.fail_first = fail_first

    def interpret(self, reference, image=None):
        if self.fail_first:
            self.fail_first = False
            raise AIError("provider_unavailable")
        self.interpreted.append(reference.id)
        return Interpretation(summary="Landing roxa", coverage="partial",
                             appearance=["fundo roxo"], colors=["roxo / purple"],
                             ui_types=["landing page"])

    def embed(self, text, query=False):
        self.embedded.append(text)
        return list(VECTOR)


def _job(ai=None, **kwargs):
    return Backfill(database(), ai, sleep=lambda _seconds: None, rate=0,
                    daily_limit=kwargs.pop("daily_limit", 5), **kwargs)


def _ready(repo, uid, *, text="Landing com fundo roxo", version="v6-post-photos",
           url=None, status="ready", analysis=True):
    if url:
        capture = Capture(kind="link", url=url, text=text)
    else:
        capture = Capture(kind="note", text=text)
    ref = repo.save(uid, str(uuid4()), capture)
    changes = {"status": status, "analysis_version": version, "embedding_space": SPACE}
    if analysis:
        changes["analysis"] = Interpretation(
            summary=text, coverage="partial", appearance=["fundo roxo"]).model_dump()
    else:
        changes["analysis"] = None
    repo.collection(uid).document(ref.id).update(changes)
    repo.job(uid, ref.id).delete()
    return ref.id


def _record(repo, uid, ref_id):
    return repo.collection(uid).document(ref_id).get().to_dict()


def test_owner_sized_library_stays_under_the_daily_provider_budget():
    pages = [{"kind": "link", "url": "https://saved.design"} for _ in range(53)]
    clips = [{"kind": "link", "url": "https://www.youtube.com/watch?v=abc", "transcript": "oi"}
             for _ in range(5)]
    full = sum(provider_usd(item, False) for item in [*pages, *clips])
    cheap = sum(provider_usd(item, True) for item in [*pages, *clips])
    assert full == pytest.approx(53 * PROVIDER_REANALYZE_USD + 5 * PROVIDER_VIDEO_USD)
    assert cheap == pytest.approx(58 * PROVIDER_REEMBED_USD)
    assert full < 1


def test_dry_run_prints_the_plan_and_writes_nothing():
    repo, uid = Repository(database()), uuid4().hex
    ref_id = _ready(repo, uid)
    before = _record(repo, uid, ref_id)
    ai = FakeAI()
    result = _job(ai).run(uid=uid)
    assert result["mode"] == "dry-run"
    assert result["completed"] == []
    assert result["planned"][0]["id"] == ref_id
    assert result["planned"][0]["action"] == "reanalyze"
    assert ai.interpreted == [] and ai.embedded == []
    assert _record(repo, uid, ref_id)["analysis_version"] == before["analysis_version"]


def test_apply_stamps_v8_and_a_second_run_skips_it():
    repo, uid = Repository(database()), uuid4().hex
    ref_id = _ready(repo, uid)
    ai = FakeAI()
    job = _job(ai)
    first = job.run(uid=uid, mode="apply")
    assert first["completed"] == [ref_id]
    saved = _record(repo, uid, ref_id)
    assert saved["analysis_version"] == ANALYSIS_VERSION
    assert saved["analysis"]["colors"] == ["roxo / purple"]
    assert saved["embedding_build"] == EMBED_BUILD
    assert saved["embedding_space"] == SPACE
    assert ai.interpreted == [ref_id]
    assert "roxo / purple" in ai.embedded[0]
    second = job.run(uid=uid, mode="apply")
    assert second["planned"] == []
    assert second["skipped"]["current"] == 1
    assert ai.interpreted == [ref_id]


def test_reembed_keeps_the_old_version_and_still_allows_a_full_run():
    repo, uid = Repository(database()), uuid4().hex
    ref_id = _ready(repo, uid)
    ai = FakeAI()
    job = _job(ai)
    done = job.run(uid=uid, mode="apply", action="reembed")
    assert done["completed"] == [ref_id]
    assert ai.interpreted == []
    assert "fundo roxo" in ai.embedded[0]
    saved = _record(repo, uid, ref_id)
    assert saved["analysis_version"] == "v6-post-photos"
    assert saved["embedding_build"] == EMBED_BUILD
    again = job.run(uid=uid, mode="apply", action="reembed")
    assert again["planned"] == []
    full = job.run(uid=uid, mode="apply", action="reanalyze")
    assert full["completed"] == [ref_id]
    assert _record(repo, uid, ref_id)["analysis_version"] == ANALYSIS_VERSION


def test_failure_of_one_item_does_not_stop_the_next():
    repo, uid = Repository(database()), uuid4().hex
    first_id = _ready(repo, uid, text="Primeira página roxa")
    second_id = _ready(repo, uid, text="Segunda página roxa")
    ai = FakeAI(fail_first=True)
    result = _job(ai).run(uid=uid, mode="apply")
    failed = {item["id"] for item in result["failed"]}
    done = set(result["completed"])
    assert failed | done == {first_id, second_id}
    assert len(failed) == 1 and len(done) == 1
    assert _record(repo, uid, next(iter(failed)))["analysis_version"] == "v6-post-photos"
    assert _record(repo, uid, next(iter(done)))["analysis_version"] == ANALYSIS_VERSION


def test_limit_force_and_other_accounts():
    repo, uid, other = Repository(database()), uuid4().hex, uuid4().hex
    _ready(repo, uid)
    _ready(repo, uid)
    current = _ready(repo, uid, version=ANALYSIS_VERSION)
    foreign = _ready(repo, other)
    preview = _job().run(uid=uid, limit=1)
    assert len(preview["planned"]) == 1
    seen = _job().run(uid=uid)
    assert current not in {item["id"] for item in seen["planned"]}
    assert foreign not in {item["id"] for item in seen["planned"]}
    assert seen["skipped"]["current"] == 1
    forced = _job().run(uid=uid, force=True)
    assert current in {item["id"] for item in forced["planned"]}
    assert foreign not in {item["id"] for item in forced["planned"]}


def test_cap_and_daily_budget_stop_before_a_call():
    repo, uid = Repository(database()), uuid4().hex
    _ready(repo, uid)
    ai = FakeAI()
    capped = _job(ai).run(uid=uid, mode="apply", max_cost_usd=0.001)
    assert capped["selected"] == []
    assert capped["stopped"] == "cap"
    assert ai.interpreted == []
    when = datetime(2030, 1, 2, tzinfo=UTC)
    database().collection("ai_budget").document("2030-01-02").set({"used": 0.99})
    blocked = Backfill(database(), ai, sleep=lambda _s: None, rate=0, daily_limit=1,
                       now=lambda: when).run(uid=uid, mode="apply")
    assert blocked["stopped"] == "budget"
    assert blocked["selected"] == []
    assert ai.interpreted == []


def test_enqueue_uses_the_configured_outbox_and_skips_a_live_job(monkeypatch):
    monkeypatch.setenv("QUEEP_JOB_COLLECTION", "jobs_testflight")
    repo, uid = Repository(database()), uuid4().hex
    ref_id = _ready(repo, uid)
    pending = _ready(repo, uid, text="Já na fila")
    repo.job(uid, pending).set({"state": "processing", "uid": uid, "reference_id": pending})
    ai = FakeAI()
    result = Backfill(database(), ai, sleep=lambda _s: None, rate=0, daily_limit=5).run(
        uid=uid, mode="enqueue", action="enqueue")
    assert result["job_collection"] == "jobs_testflight"
    assert result["completed"] == [ref_id]
    assert pending in {item["id"] for item in result["failed"]}
    assert ai.interpreted == []
    saved = _record(repo, uid, ref_id)
    assert saved["status"] == "queued"
    job = repo.job(uid, ref_id).get().to_dict()
    assert job["state"] == "pending"
    assert job["reason"] == ANALYSIS_VERSION
    assert job["revision"] == saved["revision"]


def test_deleted_and_inflight_rows_are_skipped():
    repo, uid = Repository(database()), uuid4().hex
    gone = _ready(repo, uid)
    repo.collection(uid).document(gone).update({"deleted": True})
    busy = _ready(repo, uid, status="processing")
    result = _job().run(uid=uid)
    assert result["planned"] == []
    assert result["skipped"]["deleted"] == 1
    assert result["skipped"]["in-flight"] == 1
    assert busy


def test_cli_dry_run_does_not_construct_a_paid_call(capsys):
    repo, uid = Repository(database()), uuid4().hex
    ref_id = _ready(repo, uid)
    assert main(["--uid", uid, "--dry-run", "--rate", "0"]) == 0
    output = capsys.readouterr().out
    assert "modo: dry-run" in output
    assert ref_id in output
    assert _record(repo, uid, ref_id)["analysis_version"] == "v6-post-photos"


def test_reembed_and_enqueue_cannot_be_combined():
    with pytest.raises(SystemExit):
        parse_args(["--reembed-only", "--enqueue"])
