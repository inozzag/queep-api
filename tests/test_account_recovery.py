from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from firebase_admin import auth

from queep.account_state import deletion_document
from queep.accounts import Accounts
from queep.config import Settings, database, firebase_app, image_bucket
from queep.images import Images
from queep.jobs import Jobs
from queep.models import Capture
from queep.repository import Repository
from tests.test_accounts import png


@pytest.fixture
def service():
    return Accounts(database(), image_bucket(), Settings())


def mutation(version=0, immediate=False):
    return {"operation_id": str(uuid4()), "version": version, "immediate": immediate}


def test_scheduled_deletion_preserves_auth_references_images_and_blocks_use(
    client, account, service,
):
    uid, headers = account
    repo, images = Repository(database()), Images(database(), image_bucket())
    note = repo.save(uid, str(uuid4()), Capture(kind="note", text="Recover this note"))
    picture = images.upload(uid, str(uuid4()), "Recover image", png())
    before = datetime.now(UTC)
    response = client.post("/v1/account/deletion", headers=headers, json=mutation())
    state = response.json()
    assert response.status_code == 202 and state["status"] == "scheduled"
    assert state["can_restore"]
    deadline = datetime.fromisoformat(state["delete_at"])
    assert before + timedelta(days=30) <= deadline <= datetime.now(UTC) + timedelta(days=30)
    assert auth.get_user(uid, app=firebase_app())
    assert client.get("/v1/account", headers=headers).json() == state
    assert client.get("/v1/references", headers=headers).status_code == 401
    assert client.post("/v1/search", headers=headers, json={"query": "recover"}).status_code == 401
    assert not service.process(uid, deadline - timedelta(seconds=1))
    assert repo.collection(uid).document(note.id).get().exists
    assert not images.cleanup(uid, picture.id, deadline - timedelta(seconds=1))
    assert image_bucket().blob(images.prefix(uid, picture.id, picture.image.sha256)
                               + "/original").exists()
    restored = client.post("/v1/account/deletion/cancel", headers=headers,
                           json=mutation(state["version"]))
    assert restored.status_code == 200 and restored.json()["status"] == "active"
    assert len(client.get("/v1/references", headers=headers).json()["items"]) == 2
    assert images.read(uid, picture.id)[0]
    assert service.process(uid, deadline)  # A stale maintenance invocation does nothing.
    assert repo.get(uid, note.id).text == "Recover this note"


def test_login_or_status_does_not_cancel_and_stale_requests_cannot_schedule_again(
    client, account, service,
):
    uid, headers = account
    request = mutation()
    scheduled = client.post("/v1/account/deletion", headers=headers, json=request).json()
    for _ in range(2):
        assert client.get("/v1/account", headers=headers).json() == scheduled
        assert client.post("/v1/account/deletion", headers=headers,
                           json=request).json() == scheduled
    cancel = mutation(scheduled["version"])
    active = client.post("/v1/account/deletion/cancel", headers=headers, json=cancel).json()
    assert client.post("/v1/account/deletion/cancel", headers=headers, json=cancel).json() == active
    assert client.post("/v1/account/deletion", headers=headers, json=request).status_code == 409
    assert service.get_status(uid)["status"] == "active"
    next_request = mutation(active["version"])
    next_state = client.post("/v1/account/deletion", headers=headers, json=next_request).json()
    assert next_state["status"] == "scheduled"
    assert client.post("/v1/account/deletion/cancel", headers=headers,
                       json=cancel).status_code == 409


def test_deadline_starts_erasure_and_prevents_recovery_even_before_sweep(account, service):
    uid = account[0]
    now = datetime.now(UTC)
    state = service.request(uid, now)
    assert not service.process(uid, state["delete_at"] - timedelta(microseconds=1))
    with pytest.raises(HTTPException) as e:
        service.cancel(uid, state["version"], "late-cancel", now=state["delete_at"])
    assert e.value.status_code == 409
    assert service.process(uid, state["delete_at"])
    assert service.get_status(uid)["status"] == "deleted"
    with pytest.raises(auth.UserNotFoundError):
        auth.get_user(uid, app=firebase_app())


def test_cancel_and_maintenance_transaction_cannot_erase_a_restored_account(account, service):
    uid = account[0]
    state = service.request(uid)
    instant = state["delete_at"] - timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        erased = pool.submit(service.process, uid, state["delete_at"])
        restored = pool.submit(service.cancel, uid, state["version"], "cancel", instant)
        try:
            assert restored.result()["status"] == "active"
            assert erased.result()
            assert auth.get_user(uid, app=firebase_app())
            assert service.get_status(uid)["status"] == "active"
        except HTTPException as error:
            assert error.status_code == 409
            assert erased.result()
            assert service.get_status(uid)["status"] == "deleted"
            with pytest.raises(auth.UserNotFoundError):
                auth.get_user(uid, app=firebase_app())


def test_immediate_choice_is_irreversible_and_independent_of_vendor(client, account, service):
    uid, headers = account
    state = service.request(uid)
    request = mutation(state["version"], immediate=True)
    response = client.post("/v1/account/deletion", headers=headers, json=request)
    assert response.status_code == 202 and response.json()["status"] == "deleting"
    assert not response.json()["can_restore"]
    with pytest.raises(HTTPException):
        service.cancel(uid, response.json()["version"], "too-late")
    assert client.post("/v1/account/deletion", headers=headers, json=request).status_code == 202
    with pytest.raises(auth.UserNotFoundError):
        auth.get_user(uid, app=firebase_app())


def test_recovery_requires_own_fresh_authentication(client, account, service, monkeypatch):
    uid, headers = account
    state = service.request(uid)
    change = mutation(state["version"])
    assert client.post("/v1/account/deletion/cancel", json=change).status_code == 401
    verify = auth.verify_id_token

    def old(*args, **kwargs):
        return {**verify(*args, **kwargs), "auth_time": datetime.now(UTC).timestamp() - 601}
    monkeypatch.setattr(auth, "verify_id_token", old)
    assert client.post("/v1/account/deletion/cancel", headers=headers,
                       json=change).status_code == 403
    assert service.get_status(uid)["status"] == "scheduled"


def test_paused_jobs_resume_after_restore_and_old_workers_cannot_publish(account, service):
    uid = account[0]
    repo, jobs = Repository(database()), Jobs(database())
    ref = repo.save(uid, str(uuid4()), Capture(kind="note", text="Pause and recover analysis"))
    job = repo.job(uid, ref.id)
    work = jobs.claim(jobs.payload(job.get()))
    state = service.request(uid)
    assert not jobs.current(work)
    jobs.pause(job.id)
    assert job.get().to_dict()["state"] == "pending"
    assert jobs.claim(jobs.payload(job.get())) is None
    service.cancel(uid, state["version"], "restore")
    assert not jobs.current(work)
    assert not jobs.finish(work, error="old-worker")
    resumed = jobs.claim(jobs.payload(job.get()))
    assert resumed and jobs.current(resumed)
    assert jobs.finish(resumed, error="retry")
    assert repo.get(uid, ref.id).status == "queued"


def test_restore_before_dispatcher_pauses_an_inflight_job_can_reclaim(account, service):
    uid = account[0]
    repo, jobs = Repository(database()), Jobs(database())
    ref = repo.save(uid, str(uuid4()), Capture(kind="note", text="Quick recovery"))
    job = repo.job(uid, ref.id)
    old = jobs.claim(jobs.payload(job.get()))
    state = service.request(uid)
    service.cancel(uid, state["version"], "restore")
    assert not jobs.current(old)
    assert jobs.claim(jobs.payload(job.get()))
    assert not jobs.finish(old, error="stale")


def test_expired_request_reports_no_recovery_before_maintenance(account, service):
    uid = account[0]
    state = service.request(uid)
    reported = service.get_status(uid, state["delete_at"])
    assert reported["status"] == "scheduled" and not reported["can_restore"]
    assert deletion_document(database(), uid).get().to_dict()["state"] == "scheduled"
