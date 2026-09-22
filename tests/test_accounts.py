from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from firebase_admin import auth
from google.api_core.exceptions import ServiceUnavailable
from PIL import Image

from queep.account_state import deletion_document
from queep.accounts import Accounts
from queep.billing import Billing, Event
from queep.budget import Budget
from queep.config import Settings, database, firebase_app, image_bucket
from queep.images import Images
from queep.jobs import Jobs
from queep.models import Capture
from queep.repository import Repository


@pytest.fixture
def service():
    return Accounts(database(), image_bucket(), Settings())


def png():
    data = BytesIO()
    Image.new("RGB", (24, 24), "blue").save(data, format="PNG")
    return data.getvalue()


def complete(service, uid):
    assert service.process(uid, datetime.now(UTC) + timedelta(minutes=11))
    assert set(deletion_document(database(), uid).get().to_dict()) == {
        "state", "completed_at", "version", "operation_id"}


def test_delete_requires_identity_and_recent_authentication(client, account, monkeypatch):
    uid, headers = account
    change = {"operation_id": str(uuid4()), "version": 0, "immediate": True}
    assert client.post("/v1/account/deletion", json=change).status_code == 401
    assert client.post("/v1/account/deletion",
                       headers={"Authorization": "Bearer fake"}, json=change).status_code == 401
    verify = auth.verify_id_token

    def old(*args, **kwargs):
        return {**verify(*args, **kwargs), "auth_time": int(datetime.now(UTC).timestamp()) - 601}
    monkeypatch.setattr(auth, "verify_id_token", old)
    assert client.post("/v1/account/deletion", headers=headers, json=change).status_code == 403
    assert not deletion_document(database(), uid).get().exists
    assert auth.get_user(uid, app=firebase_app())


def test_request_is_free_idempotent_and_scoped_to_verified_user(client, account, service):
    uid, headers = account
    ref_id = str(uuid4())
    repo = Repository(database())
    repo.save(uid, ref_id, Capture(kind="note", text="Private deletion fixture"))
    unrelated = "untouched-" + uuid4().hex
    repo.save(unrelated, ref_id, Capture(kind="note", text="Other account"))
    sandbox_job = database().collection("jobs_testflight").document(repo.job(uid, ref_id).id)
    sandbox_job.set(repo.job(uid, ref_id).get().to_dict())
    unrelated_sandbox_job = database().collection("jobs_testflight").document(
        repo.job(unrelated, ref_id).id)
    unrelated_sandbox_job.set(repo.job(unrelated, ref_id).get().to_dict())
    change = {"operation_id": str(uuid4()), "version": 0, "immediate": True, "uid": unrelated}
    result = client.post("/v1/account/deletion", headers=headers, json=change)
    assert result.status_code == 202 and result.json()["status"] == "deleting"
    with pytest.raises(auth.UserNotFoundError):
        auth.get_user(uid, app=firebase_app())
    for path in ["/v1/references", "/v1/billing/access", f"/v1/references/{ref_id}"]:
        assert client.get(path, headers=headers).status_code == 401
    assert client.post("/v1/account/deletion", headers=headers, json=change).status_code == 202
    assert repo.get(unrelated, ref_id).text == "Other account"
    assert not repo.collection(uid).document(ref_id).get().exists
    assert not repo.job(uid, ref_id).get().exists
    assert not sandbox_job.get().exists
    assert unrelated_sandbox_job.get().exists
    complete(service, uid)
    repo.collection(unrelated).document(ref_id).delete()
    repo.job(unrelated, ref_id).delete()
    unrelated_sandbox_job.delete()


def test_barrier_blocks_inflight_writes_workers_and_billing(account, service):
    uid = account[0]
    repo, jobs = Repository(database()), Jobs(database())
    ref = repo.save(uid, str(uuid4()), Capture(kind="note", text="Pending analysis"))
    task = jobs.payload(repo.job(uid, ref.id).get())
    work = jobs.claim(task)
    service.request(uid, immediate=True)
    for operation in [
        lambda: repo.save(uid, str(uuid4()), Capture(kind="note", text="Late")),
        lambda: jobs.retry(uid, ref.id, 1),
        lambda: Budget(database(), 1).rate_limit(uid),
        lambda: Billing(database(), Settings()).access(uid),
        lambda: repo.list(uid, 40, None),
        lambda: Images(database(), image_bucket()).upload(uid, str(uuid4()), "", png()),
    ]:
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 401
    assert not jobs.current(work)
    assert not jobs.finish(work, error="late")
    assert jobs.claim(task) is None
    assert service.resume(uid) is False
    complete(service, uid)


def test_image_and_usage_removal_survive_storage_failure(account, service, monkeypatch):
    uid = account[0]
    images = Images(database(), image_bucket())
    ref = images.upload(uid, str(uuid4()), "Private image", png())
    path = images.prefix(uid, ref.id, ref.image.sha256)
    Budget(database(), 1).rate_limit(uid)
    service.request(uid, immediate=True)
    real_blob = service.bucket.blob

    def failure(*args, **kwargs):
        raise ServiceUnavailable("injected")
    monkeypatch.setattr(service.bucket, "blob", failure)
    assert not service.resume(uid)
    assert deletion_document(database(), uid).get().to_dict()["state"] == "deleting"
    assert images.manifest(uid, ref.id).get().exists
    monkeypatch.setattr(service.bucket, "blob", real_blob)
    complete(service, uid)
    assert not list(service.bucket.list_blobs(prefix=path))
    assert not images.manifest(uid, ref.id).get().exists
    assert not list(database().collection("users").document(uid).collection("usage").stream())


def test_upload_finishing_after_request_cannot_recreate_content(account, service, monkeypatch):
    uid = account[0]
    images = Images(database(), image_bucket())
    ref_id = str(uuid4())
    put = images.put_object
    started = False

    def racing(path, data, content_type):
        nonlocal started
        put(path, data, content_type)
        if not started:
            started = True
            service.request(uid, immediate=True)
            assert not service.process(uid)
    monkeypatch.setattr(images, "put_object", racing)
    with pytest.raises(HTTPException) as error:
        images.upload(uid, ref_id, "", png())
    assert error.value.status_code == 401
    reservation = images.manifest(uid, ref_id).get().to_dict()
    assert reservation
    assert not Repository(database()).collection(uid).document(ref_id).get().exists
    complete(service, uid)
    assert not list(service.bucket.list_blobs(prefix=reservation["prefix"]))


def test_late_webhook_skips_erased_account_and_preserves_other_alias(account, service):
    uid = account[0]
    other = "alias-" + uuid4().hex
    service.request(uid, immediate=True)
    complete(service, uid)
    billing = Billing(database(), Settings())
    event = Event(id=uuid4().hex, type="RENEWAL", app_user_id=uid, aliases=[other])
    billing.event(event)
    assert not billing.document(uid).get().exists
    assert billing.document(other).get().to_dict()["generation"] == 1
    billing.document(other).delete()


@pytest.mark.parametrize("status", [200, 404])
def test_revenuecat_erasure_waits_for_absence_and_uses_verified_identity(account, service, status):
    uid = account[0]
    service.settings.revenuecat_project_id = "only-this-project"
    from pydantic import SecretStr
    service.settings.revenuecat_delete_key = SecretStr("server-only")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status if len(seen) <= 2 else 404, json={})
    service.transport = httpx.MockTransport(respond)
    service.request(uid, immediate=True)
    record = deletion_document(database(), uid).get().to_dict()
    assert service.erase_customer(uid, record) == (status == 404)
    if status == 200:
        assert seen[1].method == "DELETE"
        assert service.erase_customer(uid, record)
    complete(service, uid)
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/v2/projects/only-this-project/customers/{uid}"
    assert seen[0].headers["Authorization"] == "Bearer server-only"


def test_missing_vendor_key_does_not_block_queep_erasure(account, service):
    uid = account[0]
    service.settings.revenuecat_project_id = "configured-project"
    service.request(uid, immediate=True)
    complete(service, uid)
    pending = database().collection("provider_deletions").document(
        deletion_document(database(), uid).id)
    assert pending.get().to_dict()["uid"] == uid
    with pytest.raises(auth.UserNotFoundError):
        auth.get_user(uid, app=firebase_app())


def test_repeated_request_does_not_extend_settling_window(account, service):
    uid = account[0]
    now = datetime.now(UTC)
    service.request(uid, now, immediate=True, operation_id="repeat")
    before = deletion_document(database(), uid).get().to_dict()
    service.request(uid, now + timedelta(hours=1), immediate=True, operation_id="repeat")
    assert deletion_document(database(), uid).get().to_dict() == before
    complete(service, uid)


def test_cloud_rollout_gate_does_not_accept_new_deletions(client, account, monkeypatch):
    monkeypatch.setenv("QUEEP_ACCOUNT_DELETION_ENABLED", "false")
    uid, headers = account
    assert client.post("/v1/account/deletion", headers=headers,
                       json={"operation_id": str(uuid4()), "version": 0}).status_code == 503
    assert not deletion_document(database(), uid).get().exists


def test_avatar_round_trip_survives_replace_and_is_removed_with_the_account(
    client, account, service,
):
    uid, headers = account
    assert client.get("/v1/account/avatar", headers=headers).status_code == 204
    first = client.put("/v1/account/avatar", content=png(), headers=headers)
    assert first.status_code == 200
    assert first.json()["content_type"] in ("image/png", "image/jpeg")
    fetched = client.get("/v1/account/avatar", params={"thumbnail": False}, headers=headers)
    assert fetched.status_code == 200 and fetched.content
    second = client.put("/v1/account/avatar", content=png(), headers=headers)
    assert second.status_code == 200
    prefix = database().collection("users").document(uid).get().to_dict()["avatar_prefix"]
    assert prefix.startswith("avatars/v1/")
    assert client.delete("/v1/account/avatar", headers=headers).status_code == 204
    assert client.get("/v1/account/avatar", headers=headers).status_code == 204
    client.put("/v1/account/avatar", content=png(), headers=headers)
    prefix = database().collection("users").document(uid).get().to_dict()["avatar_prefix"]
    service.request(uid, immediate=True)
    complete(service, uid)
    assert not list(service.bucket.list_blobs(prefix=prefix))


def test_provider_requirement_cannot_disappear_with_runtime_config(account, service):
    uid = account[0]
    service.settings.revenuecat_project_id = "original-project"
    service.request(uid, immediate=True)
    service.settings.revenuecat_project_id = ""
    complete(service, uid)
    pending = database().collection("provider_deletions").document(
        deletion_document(database(), uid).id)
    assert pending.get().to_dict()["provider_project"] == "original-project"
