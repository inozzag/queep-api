from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event as Signal
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from queep.billing import Billing, Event, interpret_customer
from queep.config import Settings, database
from queep.main import app, billing

TOKEN = "queep-webhook-test-token-32-characters"
NOW = datetime.now(UTC)


def customer(active=True, **changes):
    expiry = (NOW + timedelta(days=1) if active else NOW - timedelta(days=1)).isoformat()
    subscription = {"expires_date": expiry, "purchase_date": (NOW-timedelta(days=2)).isoformat(),
                    "is_sandbox": False, "refunded_at": None,
                    "unsubscribe_detected_at": None, **changes}
    return {"subscriber": {
        "entitlements": {"queep_pro": {"product_identifier": "monthly",
                                       "expires_date": expiry,
                                       "purchase_date": subscription["purchase_date"]}},
        "subscriptions": {"monthly": subscription},
    }}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("QUEEP_BILLING_MODE", "required")
    monkeypatch.setenv("REVENUECAT_API_KEY", "test-server-key")
    monkeypatch.setenv("REVENUECAT_WEBHOOK_TOKEN", TOKEN)
    value = Billing(database(), Settings())
    app.dependency_overrides[billing] = lambda: value
    try:
        yield value
    finally:
        app.dependency_overrides.pop(billing, None)


def serve(service, payload, status=200):
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(status, json=payload)
    service.transport = httpx.MockTransport(response)
    return requests


@pytest.mark.parametrize("changes,active,renew", [
    ({}, True, True),
    ({"unsubscribe_detected_at": NOW.isoformat()}, True, False),
    ({"is_sandbox": True}, False, False),
    ({"refunded_at": NOW.isoformat()}, False, False),
])
def test_subscription_states(changes, active, renew):
    result = interpret_customer(customer(**changes), "queep_pro", False, NOW)
    assert result.active == active
    assert result.will_renew == renew


def test_expiry_grace_sandbox_and_missing_entitlement():
    assert not interpret_customer(customer(False), "queep_pro", False, NOW).active
    assert interpret_customer(customer(False, grace_period_expires_date=(
        NOW+timedelta(hours=1)).isoformat()), "queep_pro", False, NOW).active
    assert interpret_customer(customer(is_sandbox=True), "queep_pro", True, NOW).active
    assert not interpret_customer(customer(), "unowned", False, NOW).active


def test_provider_uses_verified_uid_and_cache(service, account, client):
    requests = serve(service, customer())
    uid, headers = account
    first = client.get("/v1/billing/access", headers=headers)
    assert first.status_code == 200 and first.json()["active"]
    assert requests[0].url.path.endswith(uid)
    assert requests[0].headers["Authorization"] == "Bearer test-server-key"
    # An arbitrary body cannot select another account or grant access.
    second = client.post("/v1/billing/sync", headers=headers,
                         json={"uid": "victim", "active": True})
    assert second.json() == first.json()
    assert len(requests) == 1
    assert client.get("/v1/billing/access").status_code == 401
    assert client.post("/v1/billing/sync").status_code == 401


def test_expired_account_keeps_library_but_cannot_start_paid_work(service, account, client):
    uid, headers = account
    url = f"/v1/references/{uuid4()}"
    serve(service, customer())
    note = {"kind": "note", "text": "A reference to keep"}
    assert client.put(url, json=note, headers=headers).status_code == 200
    serve(service, customer(False))
    service.event(Event(id=str(uuid4()), type="EXPIRATION", app_user_id=uid))
    assert client.get(url, headers=headers).status_code == 200
    assert client.get("/v1/references", headers=headers).status_code == 200
    for method, path, body in [
        ("put", f"/v1/references/{uuid4()}", note),
        ("patch", url, {**note, "revision": 1, "mutation_id": str(uuid4())}),
        ("post", url+"/retry", {"revision": 1}),
        ("post", "/v1/search", {"query": "A reference"}),
    ]:
        response = getattr(client, method)(path, json=body, headers=headers)
        assert response.status_code == 402, response.text
    assert client.put(url+"/image", content=b"image", headers=headers).status_code == 402
    assert client.delete(url+"?revision=1", headers=headers).status_code == 204


def test_webhooks_authenticated_bounded_and_idempotent(service, account, client):
    uid, _ = account
    payload = {"event": {"id": str(uuid4()), "type": "RENEWAL", "app_user_id": uid,
                         "future_field": "accepted"}}
    endpoint = "/v1/billing/webhook"
    assert client.post(endpoint, json=payload).status_code == 401
    assert client.post(endpoint, json=payload,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    headers = {"Authorization": "Bearer " + TOKEN}
    for _ in range(2):
        assert client.post(endpoint, json=payload, headers=headers).status_code == 200
    assert service.document(uid).get().to_dict()["generation"] == 1
    assert client.post(endpoint, content=b"x"*65537, headers=headers).status_code == 413
    assert client.post(endpoint, content=b"not json", headers=headers).status_code == 422
    assert client.post(endpoint, json={"event": {"id": "test", "type": "TEST"}},
                       headers=headers).status_code == 200


def test_transfers_invalidate_both_accounts_and_aliases(service):
    source, destination, alias = [str(uuid4()) for _ in range(3)]
    serve(service, customer())
    for uid in [source, destination, alias]:
        service.access(uid)
    service.event(Event(id=str(uuid4()), type="TRANSFER", transferred_from=[source],
                        transferred_to=[destination], aliases=[alias]))
    requests = serve(service, customer(False))
    for uid in [source, destination, alias]:
        assert not service.access(uid).active
    assert len(requests) == 3


def test_out_of_order_event_cannot_grant_or_revoke_by_itself(service, account):
    uid = account[0]
    serve(service, customer(False))
    service.event(Event(id=str(uuid4()), type="INITIAL_PURCHASE", app_user_id=uid))
    assert not service.access(uid).active
    serve(service, customer())
    service.event(Event(id=str(uuid4()), type="EXPIRATION", app_user_id=uid))
    assert service.access(uid).active


@pytest.mark.parametrize("status,payload", [(503, {}), (401, {}), (200, {}),
                                          (200, {"subscriber": {"entitlements": []}})])
def test_provider_failure_does_not_grant_access_or_hammer_provider(service, status, payload):
    requests = serve(service, payload, status)
    uid = str(uuid4())
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            service.require(uid)
        assert error.value.status_code == 503
    assert len(requests) == 1


def test_cache_cannot_extend_expiry_and_missing_key_fails_closed(service):
    uid = str(uuid4())
    serve(service, customer())
    service.access(uid)
    service.document(uid).update({"access.expires_at": NOW-timedelta(seconds=1)})
    serve(service, customer(False))
    assert not service.access(uid).active
    service.settings.revenuecat_key = None
    with pytest.raises(HTTPException) as error:
        service.require(str(uuid4()))
    assert error.value.status_code == 503


def test_event_during_refresh_discards_old_snapshot_and_newer_lease(service):
    uid = str(uuid4())
    started, finish = Signal(), Signal()

    def response(request):
        started.set()
        assert finish.wait(5)
        return httpx.Response(200, json=customer())
    service.transport = httpx.MockTransport(response)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(service.access, uid)
        assert started.wait(5)
        # Concurrent access is limited to one provider call per account.
        with pytest.raises(HTTPException):
            service.access(uid)
        service.event(Event(id=str(uuid4()), type="REFUND", app_user_id=uid))
        serve(service, customer(False))
        assert not service.access(uid).active
        finish.set()
        with pytest.raises(HTTPException):
            old.result()
    assert not service.access(uid).active


def test_development_bypass_is_explicit_and_forbidden_in_production(monkeypatch):
    monkeypatch.setenv("QUEEP_BILLING_MODE", "development")
    service = Billing(database(), Settings())
    assert service.access(str(uuid4())).mode == "development"
    for name in ["FIREBASE_AUTH_EMULATOR_HOST", "FIRESTORE_EMULATOR_HOST",
                 "FIREBASE_STORAGE_EMULATOR_HOST", "STORAGE_EMULATOR_HOST"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUEEP_ENV", "production")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "queep-production")
    with pytest.raises(ValueError, match="development project"):
        Settings()
    monkeypatch.setenv("QUEEP_BILLING_MODE", "required")
    monkeypatch.setenv("QUEEP_BILLING_ACCEPT_SANDBOX", "true")
    with pytest.raises(ValueError, match="development project"):
        Settings()
