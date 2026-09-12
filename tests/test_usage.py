from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import ServiceUnavailable

from queep.budget import Budget
from queep.config import Settings, database, image_bucket
from queep.images import Images
from queep.jobs import Jobs
from queep.media import MediaFiles
from queep.models import Capture
from queep.repository import Repository
from queep.usage import document, snapshot
from tests.test_images import picture
from tests.test_media import wav


def test_limits_are_private_and_readable_without_paid_access(client, account, monkeypatch):
    uid, headers = account
    monkeypatch.setenv("QUEEP_BILLING_MODE", "required")
    assert client.get("/v1/usage").status_code == 401
    result = client.get("/v1/usage", headers=headers)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["counters"]["operations"] == {"used": 0, "limit": 100}


def test_concurrent_operations_cannot_overrun_limit(account, monkeypatch):
    uid, _ = account
    monkeypatch.setenv("QUEEP_OPERATION_DAILY_LIMIT", "3")
    repo = Repository(database())

    def save(ref_id):
        try:
            repo.save(uid, ref_id, Capture(kind="note", text="Synthetic quota test"))
            return 200
        except HTTPException as error:
            assert error.status_code in (429, 503)
            assert int(error.headers["Retry-After"]) > 0
            return error.status_code

    identifiers = [str(uuid4()) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, identifiers))
    # Firestore may exhaust contention retries. Clients can safely resend that same ID.
    results = [save(identifier) if result == 503 else result
               for identifier, result in zip(identifiers, results, strict=True)]
    assert results.count(200) == 3
    assert len(repo.list(uid, 10, None).items) == 3
    assert snapshot(database(), uid, Settings())["counters"]["operations"]["used"] == 3


def test_exact_retries_edit_and_deletion_at_limit(client, account, monkeypatch):
    uid, headers = account
    monkeypatch.setenv("QUEEP_OPERATION_DAILY_LIMIT", "2")
    path = f"/v1/references/{uuid4()}"
    capture = {"kind": "note", "text": "Synthetic original"}
    assert client.put(path, headers=headers, json=capture).status_code == 200
    edited = capture | {"text": "Synthetic edit", "revision": 1, "mutation_id": str(uuid4())}
    assert client.patch(path, headers=headers, json=edited).status_code == 200
    assert client.patch(path, headers=headers, json=edited).status_code == 200
    assert client.put(path, headers=headers, json=capture).status_code == 200
    assert client.put(f"/v1/references/{uuid4()}", headers=headers, json=capture).status_code == 429
    assert client.delete(path + "?revision=2", headers=headers).status_code == 204
    # Deleting cannot reset processing/storage budgets.
    assert snapshot(database(), uid, Settings())["counters"]["operations"]["used"] == 2


def test_failed_upload_reserves_once_and_retry_remains_available(account, monkeypatch):
    uid, _ = account
    monkeypatch.setenv("QUEEP_OPERATION_DAILY_LIMIT", "1")
    service = Images(database(), image_bucket())
    ref_id = str(uuid4())
    original = service.put_object
    monkeypatch.setattr(
        service, "put_object", lambda *args: (_ for _ in ()).throw(ServiceUnavailable("test"))
    )
    with pytest.raises(HTTPException, match="Upload não confirmado"):
        service.upload(uid, ref_id, "", picture())
    before = snapshot(database(), uid, Settings())["counters"]
    monkeypatch.setattr(service, "put_object", original)
    service.upload(uid, ref_id, "", picture())
    assert snapshot(database(), uid, Settings())["counters"] == before
    assert before["operations"]["used"] == 1
    assert before["upload_bytes"]["used"] > 0


def test_upload_budget_blocks_before_storage_or_placeholder(account, monkeypatch):
    uid, _ = account
    now = datetime.now(UTC)
    document(database(), uid, now).set({"upload_bytes": 100 * 1024 * 1024})
    service = Images(database(), image_bucket())
    ref_id = str(uuid4())
    monkeypatch.setattr(service, "put_object", lambda *_: pytest.fail("Must not write Storage"))
    with pytest.raises(HTTPException) as error:
        service.upload(uid, ref_id, "", picture())
    assert error.value.status_code == 429
    assert not service.manifest(uid, ref_id).get().exists
    assert not service.repo.collection(uid).document(ref_id).get().exists


def test_media_time_and_manual_retry_share_daily_allowance(account, monkeypatch):
    uid, _ = account
    monkeypatch.setenv("QUEEP_MEDIA_DAILY_MINUTES", "1")
    service = MediaFiles(database(), image_bucket())
    ref_id = str(uuid4())
    service.upload(uid, ref_id, "", wav(31))
    service.repo.collection(uid).document(ref_id).update({"status": "error"})
    with pytest.raises(HTTPException) as error:
        Jobs(database()).retry(uid, ref_id, 1)
    assert error.value.status_code == 429
    counters = snapshot(database(), uid, Settings())["counters"]
    assert 31 <= counters["media_seconds"]["used"] <= 32
    assert counters["operations"]["used"] == 1
    assert service.repo.get(uid, ref_id).status == "error"


def test_search_updates_preserve_upload_counters_and_accounts_are_isolated(account):
    uid, _ = account
    now = datetime.now(UTC)
    doc = document(database(), uid, now)
    doc.set({"operations": 7, "upload_bytes": 42, "media_seconds": 90})
    Budget(database(), 1, search_daily_limit=1).rate_limit(uid)
    with pytest.raises(HTTPException) as error:
        Budget(database(), 1, search_daily_limit=1).rate_limit(uid)
    assert "diário" in error.value.detail
    counters = snapshot(database(), uid, Settings())["counters"]
    assert [
        counters[key]["used"] for key in ("operations", "upload_bytes", "media_seconds", "searches")
    ] == [7, 42, 90, 1]
    assert doc.get().to_dict()["expires_at"] > now + timedelta(days=29)
    for record in (
        snapshot(database(), str(uuid4()), Settings()),
        snapshot(database(), uid, Settings(), now=now + timedelta(days=1)),
    ):
        assert all(counter["used"] == 0 for counter in record["counters"].values())
