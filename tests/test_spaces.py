from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from queep.config import database
from queep.models import SpaceMembership, SpaceName
from queep.repository import Repository
from queep.spaces import Spaces
from queep.usage import snapshot
from tests.test_api import NOTE
from tests.test_plans import change_plan, plan  # noqa: F401


def create(client, headers, name="Cliente A"):
    response = client.post("/v1/spaces", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def capture(client, headers, spaces=()):
    response = client.put(f"/v1/references/{uuid4()}", headers=headers,
                          json={**NOTE, "space_ids": list(spaces)})
    assert response.status_code == 200, response.text
    return response.json()


def test_crud_names_reorder_and_usage(client, account):
    uid, headers = account
    a = create(client, headers, "  Cliente A  ")
    b = create(client, headers, "🎨 Moodboard")
    assert a["name"] == "Cliente A"
    assert client.post("/v1/spaces", headers=headers,
                       json={"name": " CLIENTE a "}).status_code == 409
    for name in ["", "  ", "a" * 41]:
        assert client.post("/v1/spaces", headers=headers,
                           json={"name": name}).status_code == 422
    response = client.patch(f"/v1/spaces/{b['id']}", headers=headers,
                            json={"name": "Pessoal", "sort_index": 0})
    assert response.status_code == 200, response.text
    assert [r["name"] for r in client.get("/v1/spaces", headers=headers).json()] == [
        "Pessoal", "Cliente A"]
    assert client.patch(f"/v1/spaces/{b['id']}", headers=headers,
                        json={"name": "cliente a"}).status_code == 409
    from queep.config import Settings
    used = snapshot(database(), uid, Settings())["counters"]
    assert used["spaces"]["used"] == 2
    assert used["operations"]["used"] == 0


def test_capture_multi_membership_archive_and_safe_delete(client, account):
    uid, headers = account
    a, b = create(client, headers), create(client, headers, "Pessoal")
    ref = capture(client, headers, [a["id"]])
    inbox = capture(client, headers)
    path = f"/v1/references/{ref['id']}/spaces"
    for _ in range(2):
        response = client.post(path, headers=headers, json={"add": [b["id"], b["id"]]})
        assert response.status_code == 200, response.text
        assert set(response.json()["space_ids"]) == {a["id"], b["id"]}
        assert response.json()["revision"] == ref["revision"]
    for space in (a, b):
        assert [r["id"] for r in client.get("/v1/references", headers=headers,
                params={"space_id": space["id"]}).json()["items"]] == [ref["id"]]
    assert len(client.get("/v1/references", headers=headers).json()["items"]) == 2
    assert {s["reference_count"] for s in client.get("/v1/spaces", headers=headers).json()} == {1}
    Repository(database()).collection(uid).document(ref["id"]).update({
        "archived_at": datetime.now(UTC)})
    assert client.get("/v1/references", headers=headers,
                      params={"space_id": a["id"]}).json()["items"] == []
    assert {s["reference_count"] for s in client.get("/v1/spaces", headers=headers).json()} == {0}
    Repository(database()).collection(uid).document(ref["id"]).update({"archived_at": None})
    assert client.delete(f"/v1/spaces/{a['id']}", headers=headers).status_code == 204
    saved = client.get(f"/v1/references/{ref['id']}", headers=headers).json()
    assert saved["space_ids"] == [b["id"]]
    assert client.get(f"/v1/references/{inbox['id']}", headers=headers).status_code == 200
    assert client.post(path, headers=headers, json={"remove": [b["id"]]}).json()["space_ids"] == []
    assert client.post(path, headers=headers, json={"add": [a["id"]]}).status_code == 404


@pytest.mark.parametrize("value,cap,target", [("free", 0, "pro"), ("pro", 15, "max"),
                                             ("max", 40, None)])
def test_plan_caps_and_assignment_after_downgrade(client, account, plan, value, cap, target):  # noqa: F811
    uid, headers = account
    plan["value"] = value
    spaces = [create(client, headers, f"Space {i}") for i in range(cap)]
    response = client.post("/v1/spaces", headers=headers, json={"name": "Acima do limite"})
    assert response.status_code == 402, response.text
    assert response.json()["detail"]["required_plan"] == target
    if spaces:
        ref = capture(client, headers)
        change_plan(plan, uid, "free")
        result = client.post(f"/v1/references/{ref['id']}/spaces", headers=headers,
                             json={"add": [spaces[0]["id"]]})
        assert result.status_code == 200, result.text


def test_capture_and_edit_preserve_membership_and_retries(client, account):
    _, headers = account
    a = create(client, headers)
    ref = capture(client, headers, [a["id"]])
    path = f"/v1/references/{ref['id']}"
    payload = {**NOTE, "space_ids": [a["id"]]}
    assert client.put(path, headers=headers, json=payload).json() == ref
    response = client.patch(path, headers=headers, json={**NOTE, "text": "Revisada",
                            "revision": 1, "mutation_id": str(uuid4())})
    assert response.status_code == 200, response.text
    assert response.json()["space_ids"] == [a["id"]]
    assert client.put(f"/v1/references/{uuid4()}", headers=headers,
                      json={**NOTE, "space_ids": [str(uuid4())]}).status_code == 404


def test_isolation_unknown_spaces_and_cursor_scope(client, account):
    _, headers = account
    space = create(client, headers)
    service = Spaces(database())
    with pytest.raises(HTTPException) as error:
        service.require(uuid4().hex, [space["id"]])
    assert error.value.status_code == 404
    assert client.get("/v1/spaces").status_code == 401
    for _ in range(3):
        capture(client, headers, [space["id"]])
    first = client.get("/v1/references", headers=headers,
                       params={"space_id": space["id"], "limit": 1}).json()
    assert first["next_cursor"]
    assert client.get("/v1/references", headers=headers,
                      params={"cursor": first["next_cursor"]}).status_code == 400
    second = client.get("/v1/references", headers=headers, params={
        "space_id": space["id"], "cursor": first["next_cursor"], "limit": 10}).json()
    assert len(second["items"]) == 2


def test_concurrent_names_and_membership(account):
    uid, _ = account
    service = Spaces(database())

    def save(_):
        try:
            service.save(uid, SpaceName(name="Mesmo nome"))
            return 201
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(save, range(4)))
    assert outcomes.count(201) == 1
    assert set(outcomes) <= {201, 409, 503}
    from queep.models import Capture
    ref = service.repo.save(uid, str(uuid4()), Capture(**NOTE))
    a = service.list(uid)[0]
    b = service.save(uid, SpaceName(name="Segundo"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda s: service.assign(uid, ref.id, SpaceMembership(add=[s.id])), [a, b]))
    assert set(service.repo.get(uid, ref.id).space_ids) == {a.id, b.id}


def test_cleanup_resumes_after_large_delete(account):
    uid, _ = account
    service = Spaces(database())
    space = service.save(uid, SpaceName(name="Grande"))
    from queep.models import Capture
    template = service.repo.save(uid, str(uuid4()), Capture(**NOTE))
    for start in range(0, 505, 400):
        batch = database().batch()
        for _ in range(start, min(start + 400, 505)):
            batch.set(service.repo.collection(uid).document(str(uuid4())), {
                **template.model_dump(), "space_ids": [space.id], "deleted": False})
        batch.commit()
    service.delete(uid, space.id)
    assert service.list(uid) == []
    service.sweep()
    service.cleanup(uid, space.id)
    records = list(service.repo.collection(uid).stream())
    assert len(records) == 506
    assert all(space.id not in r.to_dict().get("space_ids", []) for r in records)
    assert not service.collection(uid).document(space.id).get().exists


def test_search_scopes_candidates_and_rechecks_removed_membership(client, account):
    from types import SimpleNamespace

    from queep.jobs import Jobs
    from queep.search import search
    from tests.test_ai_processing import VECTOR, publish

    uid, headers = account
    space = create(client, headers)
    selected = capture(client, headers, [space["id"]])
    outside = capture(client, headers)
    service = Spaces(database())
    for ref in (selected, outside):
        publish(service.repo, Jobs(database()), uid, service.repo.get(uid, ref["id"]))
    seen = []

    def verify(query, refs):
        seen.extend(r.id for r in refs)
        service.assign(uid, selected["id"], SpaceMembership(remove=[space["id"]]))
        return [r.id for r in refs]

    ai = SimpleNamespace(budget=SimpleNamespace(rate_limit=lambda _: None),
                         embed=lambda *_, **__: VECTOR, verify=verify)
    assert search(service.repo, ai, uid, "CRM branco", space["id"]).items == []
    assert seen == [selected["id"]]


def test_image_capture_starts_in_space_and_retries_do_not_restore_removed_membership(
    client, account,
):
    from io import BytesIO

    from PIL import Image

    _, headers = account
    space = create(client, headers)
    data = BytesIO()
    Image.new("RGB", (16, 16), "red").save(data, "PNG")
    path = f"/v1/references/{uuid4()}"
    params = {"space_ids": space["id"]}
    first = client.put(path + "/image", content=data.getvalue(), params=params, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["space_ids"] == [space["id"]]
    assert client.post(path + "/spaces", headers=headers,
                       json={"remove": [space["id"]]}).status_code == 200
    retry = client.put(path + "/image", content=data.getvalue(), params=params, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["space_ids"] == []
