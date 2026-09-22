from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from queep.billing import Access, Billing, Event, interpret_customer
from queep.budget import Budget
from queep.config import Settings, database, image_bucket
from queep.media import MediaFiles
from queep.models import Capture
from queep.plans import LIMITS, month_window
from queep.repository import Repository
from queep.usage import document, inventory_document, snapshot
from tests.test_billing import NOW, customer
from tests.test_media import wav


@pytest.fixture
def plan(monkeypatch):
    current = {"value": "free"}
    monkeypatch.setenv("QUEEP_BILLING_MODE", "required")
    monkeypatch.setattr(Billing, "customer", lambda self, uid, now: Access(
        active=current["value"] != "free", plan=current["value"], checked_at=now,
        expires_at=now + timedelta(days=30) if current["value"] != "free" else None))
    return current


def change_plan(plan, uid, value):
    plan["value"] = value
    Billing(database(), Settings()).event(Event(id=str(uuid4()), type="TEST", app_user_id=uid))


@pytest.mark.parametrize("value", ["free", "pro", "max"])
def test_contract_and_monthly_usage(client, account, plan, value):
    uid, headers = account
    plan["value"] = value
    access = client.get("/v1/billing/access", headers=headers).json()
    assert access["plan"] == value
    assert access["limits"] == LIMITS[value]
    assert access["features"] == {"spaces": value != "free", "media_av": value == "max"}
    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["plan"] == value
    assert {k: v["limit"] for k, v in usage["counters"].items()} == LIMITS[value]


def test_free_thirteenth_capture_and_extra_search_offer_pro(client, account, plan):
    uid, headers = account
    for _ in range(12):
        assert client.put(f"/v1/references/{uuid4()}", headers=headers,
                          json={"kind": "note", "text": "quota"}).status_code == 200
    response = client.put(f"/v1/references/{uuid4()}", headers=headers,
                          json={"kind": "link", "url": "https://example.com"})
    assert response.status_code == 402
    assert response.json()["detail"]["required_plan"] == "pro"
    now = datetime.now(UTC)
    document(database(), uid, now).set({"searches": 8}, merge=True)
    with pytest.raises(HTTPException) as error:
        Budget(database(), 1).rate_limit(uid)
    assert error.value.status_code == 402
    assert error.value.detail["required_plan"] == "pro"


@pytest.mark.parametrize("value,target", [("free", "pro"), ("pro", "max"), ("max", None)])
def test_monthly_limit_upgrades_and_does_not_create_ghosts(client, account, plan, value, target):
    uid, headers = account
    plan["value"] = value
    document(database(), uid, datetime.now(UTC)).set({"operations": LIMITS[value]["operations"]})
    path = f"/v1/references/{uuid4()}"
    response = client.put(path, headers=headers, json={"kind": "note", "text": "quota"})
    assert response.status_code == 402
    assert response.json()["detail"]["required_plan"] == target
    assert client.get(path, headers=headers).status_code == 404


@pytest.mark.parametrize("value", ["free", "pro"])
def test_media_denied_before_decoding_or_placeholder(client, account, plan, value, monkeypatch):
    uid, headers = account
    plan["value"] = value
    monkeypatch.setattr(MediaFiles, "prepare", lambda *_: pytest.fail("Must not decode media"))
    path = f"/v1/references/{uuid4()}"
    response = client.put(path + "/media", content=b"not even media", headers=headers)
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "media_av_required"
    assert response.json()["detail"]["required_plan"] == "max"
    assert client.get(path, headers=headers).status_code == 404
    assert snapshot(database(), uid, Settings())["counters"]["items"]["used"] == 0


def test_downgrade_keeps_existing_media_readable(client, account, plan):
    uid, headers = account
    plan["value"] = "max"
    path = f"/v1/references/{uuid4()}"
    response = client.put(path + "/media", content=wav(2), headers=headers)
    assert response.status_code == 200, response.text
    assert snapshot(database(), uid, Settings())["counters"]["media_seconds"]["used"] >= 2
    change_plan(plan, uid, "pro")
    assert client.get(path + "/media", headers=headers).status_code == 200
    blocked = client.put(f"/v1/references/{uuid4()}/media", content=wav(), headers=headers)
    assert blocked.status_code == 402
    assert client.delete(path + "?revision=1", headers=headers).status_code == 204


def test_storage_and_item_cap_are_atomic(account, plan):
    uid, _ = account
    repo = Repository(database())
    inventory_document(database(), uid).set({"items": 99, "storage_bytes": 0, "spaces": 0})
    def save(_):
        try:
            repo.save(uid, str(uuid4()), Capture(kind="note", text="quota"))
            return 200
        except HTTPException as error:
            return error.status_code
    # Warm the provider cache before concurrency.
    Billing(database(), Settings()).access(uid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(save, range(4)))
    assert statuses.count(200) == 1
    assert set(statuses) <= {200, 402, 503}
    inventory_document(database(), uid).update({"items": 1, "storage_bytes": 251 * 1024**2})
    with pytest.raises(HTTPException) as error:
        repo.save(uid, str(uuid4()), Capture(kind="note", text="quota"))
    assert error.value.detail["code"] == "storage_limit"


def test_month_boundaries_and_upgrade_do_not_reset_usage(account, plan):
    uid, _ = account
    before = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    after = datetime(2027, 1, 1, tzinfo=UTC)
    assert month_window(before)[1] == after
    assert month_window(datetime(2028, 2, 29, tzinfo=UTC))[1].day == 1
    document(database(), uid, before).set({"operations": 12})
    assert snapshot(database(), uid, Settings(), before)["counters"]["operations"]["used"] == 12
    assert snapshot(database(), uid, Settings(), after)["counters"]["operations"]["used"] == 0
    change_plan(plan, uid, "pro")
    usage = snapshot(database(), uid, Settings(), before)
    assert usage["counters"]["operations"] == {"used": 12, "limit": 100}


def test_max_wins_but_expired_max_falls_back_to_active_pro():
    data = customer()
    data["subscriber"]["entitlements"]["queep_max"] = dict(
        data["subscriber"]["entitlements"]["queep_pro"])
    assert interpret_customer(data, "queep_pro", False, NOW).plan == "max"
    data["subscriber"]["entitlements"]["queep_max"]["expires_date"] = (
        NOW - timedelta(seconds=1)).isoformat()
    assert interpret_customer(data, "queep_pro", False, NOW).plan == "pro"
    assert interpret_customer(customer(False), "queep_pro", False, NOW).plan == "free"


def test_space_policy_downgrade_and_assignment_at_limit():
    from queep.plans import require_space_change
    for value, count, target in [("free", 0, "pro"), ("pro", 15, "max"), ("max", 40, None)]:
        with pytest.raises(HTTPException) as error:
            require_space_change(value, count, creating=True)
        assert error.value.detail["required_plan"] == target
    require_space_change("pro", 40, creating=False)
    with pytest.raises(HTTPException):
        require_space_change("free", 15, creating=False)


def test_margin_aggregates_calls_and_never_treats_unknown_cost_as_zero():
    from queep.margin import summarize
    rows = [dict(operation_id="one", plan="max", kind="media", media_seconds=60, cost_usd=.04),
            dict(operation_id="one", plan="max", kind="media", media_seconds=60, cost_usd=.03),
            dict(operation_id="two", plan="max", kind="media", media_seconds=60, cost_usd=None)]
    group = summarize(rows)["groups"][0]
    assert group["operations"] == 2
    assert group["operations_with_known_cost"] == 1
    assert group["average_media_minute_usd"] == pytest.approx(.07)
    assert group["review_quotas"]


def test_previews_count_thumbnails_and_delete_cleans_unpublished_reservations(account, plan):
    from queep.images import Images, prepare
    from tests.test_images import picture
    uid, _ = account
    repo = Repository(database())
    service = Images(database(), image_bucket())
    ref_id = str(uuid4())
    repo.save(uid, ref_id, Capture(kind="link", url="https://example.com"))
    image = prepare(picture())
    size = len(image.original) + len(image.thumbnail)
    # Simulate a worker interrupted after reserving but before publishing the preview.
    service.reserve_preview(uid, ref_id, image)
    assert snapshot(database(), uid, Settings())["counters"]["storage_bytes"]["used"] == size
    service.reserve_preview(uid, ref_id, image)
    assert snapshot(database(), uid, Settings())["counters"]["storage_bytes"]["used"] == size
    repo.delete(uid, ref_id, 1)
    service.cleanup(uid, ref_id, now=datetime.now(UTC) + timedelta(minutes=11))
    usage = snapshot(database(), uid, Settings())["counters"]
    assert usage["items"]["used"] == 0
    assert usage["storage_bytes"]["used"] == 0
    assert usage["operations"]["used"] == 1


def test_storage_downgrade_blocks_new_upload_but_not_read_delete(client, account, plan):
    from tests.test_images import picture
    uid, headers = account
    plan["value"] = "max"
    path = f"/v1/references/{uuid4()}"
    assert client.put(path + "/image", content=picture(), headers=headers).status_code == 200
    inventory_document(database(), uid).update({"storage_bytes": 3 * 1024**3})
    change_plan(plan, uid, "pro")
    blocked = client.put(f"/v1/references/{uuid4()}/image", content=picture(), headers=headers)
    assert blocked.status_code == 402
    assert blocked.json()["detail"]["code"] == "storage_limit"
    assert client.get(path + "/image", headers=headers).status_code == 200
    assert client.get("/v1/references", headers=headers).status_code == 200
    assert client.delete(path + "?revision=1", headers=headers).status_code == 204
