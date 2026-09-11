from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from queep.config import database
from queep.main import app
from queep.models import Capture, Interpretation
from queep.repository import Repository


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def account():
    response = httpx.post(
        "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:signUp?key=local",
        json={"email": f"{uuid4()}@example.test", "password": "queep-testing-123",
              "returnSecureToken": True},
    )
    response.raise_for_status()
    data = response.json()
    return data["localId"], {"Authorization": f"Bearer {data['idToken']}"}


def path(ref_id=None):
    return f"/v1/references/{ref_id or uuid4()}"


NOTE = {"kind": "note", "title": "Ideias", "text": "Interface CRM com fundo branco"}


def test_requires_authentication_and_rejects_invalid_tokens(client):
    for headers in ({}, {"Authorization": "Bearer invalid"}):
        assert client.get("/v1/references", headers=headers).status_code == 401
        assert client.put(path(), json=NOTE, headers=headers).status_code == 401


def test_two_sessions_share_persistent_library(client, account):
    _, headers = account
    url = path()
    first = client.put(url, json=NOTE, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "queued"
    with TestClient(app) as other_device:
        retrieved = other_device.get(url, headers=headers)
        assert retrieved.json() == first.json()
    assert client.get("/v1/references", headers=headers).json()["items"] == [first.json()]
    assert first.headers["Cache-Control"] == "no-store"


def test_repeated_create_is_idempotent_and_rejects_reused_id(client, account):
    _, headers = account
    url = path()
    first = client.put(url, json=NOTE, headers=headers)
    second = client.put(url, json=NOTE, headers=headers)
    assert first.json() == second.json()
    assert client.put(url, json={**NOTE, "text": "different"}, headers=headers).status_code == 409
    assert len(client.get("/v1/references", headers=headers).json()["items"]) == 1


def test_accounts_cannot_read_edit_or_delete_each_others_data(client, account):
    _, headers = account
    url = path()
    client.put(url, json=NOTE, headers=headers)
    # A separate, verified Firebase identity cannot select another owner through the request.
    r = httpx.post(
        "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:signUp?key=local",
        json={"returnSecureToken": True},
    )
    other = {"Authorization": f"Bearer {r.json()['idToken']}"}
    assert client.get(url, headers=other).status_code == 404
    edit = {**NOTE, "revision": 1, "mutation_id": str(uuid4())}
    assert client.patch(url, json=edit, headers=other).status_code == 404
    assert client.delete(url + "?revision=1", headers=other).status_code == 204
    assert client.get(url, headers=headers).status_code == 200
    assert client.get("/v1/references", headers=other).json()["items"] == []
    assert client.put(path(), json={**NOTE, "uid": "someone"}, headers=other).status_code == 422


def test_edit_retry_and_stale_revision(client, account):
    _, headers = account
    url = path()
    client.put(url, json=NOTE, headers=headers)
    edit = {**NOTE, "text": "Nova nota", "revision": 1, "mutation_id": str(uuid4())}
    first = client.patch(url, json=edit, headers=headers)
    assert first.status_code == 200
    assert first.json()["revision"] == 2
    assert client.patch(url, json=edit, headers=headers).json() == first.json()
    stale = {**edit, "mutation_id": str(uuid4())}
    assert client.patch(url, json=stale, headers=headers).status_code == 409
    assert client.delete(url + "?revision=1", headers=headers).status_code == 409


def test_deleted_reference_cannot_be_resurrected_by_retry_or_worker(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    url = path(ref_id)
    client.put(url, json=NOTE, headers=headers)
    assert client.delete(url + "?revision=1", headers=headers).status_code == 204
    assert client.delete(url + "?revision=1", headers=headers).status_code == 204
    assert client.put(url, json=NOTE, headers=headers).status_code == 409
    repo = Repository(database())
    assert not repo.complete(uid, ref_id, 1, Interpretation(summary="old", coverage="full"))
    assert client.get(url, headers=headers).status_code == 404
    assert not repo.job(uid, ref_id).get().exists
    assert "text" not in repo.collection(uid).document(ref_id).get().to_dict()


def test_old_analysis_cannot_replace_edited_content(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    url = path(ref_id)
    client.put(url, json=NOTE, headers=headers)
    edit = {**NOTE, "text": "Revised", "revision": 1, "mutation_id": str(uuid4())}
    client.patch(url, json=edit, headers=headers)
    repo = Repository(database())
    analysis = Interpretation(summary="old", coverage="full")
    assert not repo.complete(uid, ref_id, 1, analysis)
    assert repo.job(uid, ref_id).get().to_dict()["revision"] == 2
    assert client.get(url, headers=headers).json()["analysis"] is None


def test_concurrent_retries_create_one_document_and_one_job(account):
    uid, _ = account
    ref_id = str(uuid4())
    repo = Repository(database())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: repo.save(uid, ref_id, Capture(**NOTE)), range(12)))
    assert all(r.revision == 1 for r in results)
    assert len(list(repo.collection(uid).stream())) == 1
    assert repo.job(uid, ref_id).get().to_dict()["revision"] == 1


def test_pagination_and_deleted_items(client, account):
    _, headers = account
    for i in range(5):
        client.put(path(), json={**NOTE, "title": str(i)}, headers=headers)
    ids, cursor = [], None
    for _ in range(3):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/references", params=params, headers=headers).json()
        ids += [r["id"] for r in page["items"]]
        cursor = page["next_cursor"]
    assert len(ids) == len(set(ids)) == 5
    assert cursor is None
    assert client.get("/v1/references?cursor=garbage", headers=headers).status_code == 400


@pytest.mark.parametrize("payload", [
    {"kind": "note", "text": "  "},
    {"kind": "link", "url": "javascript:alert(1)"},
    {"kind": "link", "url": "https://user:password@example.com"},
    {"kind": "note", "text": "a" * 30001},
    {"kind": "note", "text": "test", "status": "ready"},
])
def test_invalid_capture(client, account, payload):
    assert client.put(path(), json=payload, headers=account[1]).status_code == 422


def test_link_is_preserved_without_claiming_full_content_analysis(client, account):
    response = client.put(path(), json={"kind": "link", "url": "https://www.instagram.com/p/example/"},
                          headers=account[1])
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["analysis"] is None
