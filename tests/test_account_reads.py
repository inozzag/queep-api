from uuid import uuid4

import pytest
from fastapi import HTTPException
from firebase_admin import auth, exceptions
from google.api_core.exceptions import (
    Aborted,
    DeadlineExceeded,
    FailedPrecondition,
    RetryError,
    ServiceUnavailable,
)

from queep.availability import auth_retry, firestore_retry


class _Broken:
    def __init__(self, error):
        self.error = error

    def get(self, transaction=None):
        raise self.error


def _auth_error(kind):
    if kind == "certificate":
        return auth.CertificateFetchError("certs", cause=ConnectionError("down"))
    if kind == "unavailable":
        return exceptions.UnavailableError("identity toolkit")
    if kind == "deadline":
        return exceptions.DeadlineExceededError("timeout")
    return exceptions.InternalError("identity toolkit")


def _firestore_error(kind):
    if kind == "unavailable":
        return ServiceUnavailable("busy")
    if kind == "deadline":
        return DeadlineExceeded("slow")
    return Aborted("contention")


def _assert_retry(response):
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["detail"] == "Serviço ocupado. Tente novamente."


@pytest.mark.parametrize("path", ["/v1/account", "/v1/usage"])
@pytest.mark.parametrize("kind", ["certificate", "unavailable", "deadline", "internal"])
def test_transient_auth_is_retryable(client, account, monkeypatch, path, kind):
    error = _auth_error(kind)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(auth, "verify_id_token", fail)
    _assert_retry(client.get(path, headers=account[1]))


@pytest.mark.parametrize("path", ["/v1/account", "/v1/usage"])
@pytest.mark.parametrize("error", [
    auth.InvalidIdTokenError("bad"),
    auth.RevokedIdTokenError("revoked"),
    auth.UserDisabledError("disabled"),
    ValueError("empty"),
])
def test_rejected_auth_stays_unauthorized(client, account, monkeypatch, path, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(auth, "verify_id_token", fail)
    response = client.get(path, headers=account[1])
    assert response.status_code == 401
    assert response.json()["detail"] == "Sessão inválida"
    assert "Retry-After" not in response.headers
    if path == "/v1/usage":
        assert response.headers["www-authenticate"] == "Bearer"
    else:
        assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("path", ["/v1/account", "/v1/usage"])
def test_missing_session_stays_unauthorized(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["detail"] == "Entre na sua conta"
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("kind", ["unavailable", "deadline", "aborted"])
def test_transient_account_read_is_retryable(client, account, monkeypatch, kind):
    monkeypatch.setattr("queep.accounts.deletion_document",
                        lambda *_args, **_kwargs: _Broken(_firestore_error(kind)))
    _assert_retry(client.get("/v1/account", headers=account[1]))


def test_exhausted_account_read_is_retryable(client, account, monkeypatch):
    class Exhausted:
        def get(self, transaction=None):
            raise ValueError("Failed to commit transaction") from Aborted("contention")

    monkeypatch.setattr("queep.accounts.deletion_document", lambda *_args, **_kwargs: Exhausted())
    _assert_retry(client.get("/v1/account", headers=account[1]))


def test_non_transient_account_read_is_not_retryable(client, account, monkeypatch):
    monkeypatch.setattr("queep.accounts.deletion_document",
                        lambda *_args, **_kwargs: _Broken(FailedPrecondition("index")))
    with pytest.raises(FailedPrecondition):
        client.get("/v1/account", headers=account[1])


@pytest.mark.parametrize("kind", ["unavailable", "deadline", "aborted"])
def test_transient_usage_barrier_is_retryable(client, account, monkeypatch, kind):
    monkeypatch.setattr("queep.account_state.deletion_document",
                        lambda *_args, **_kwargs: _Broken(_firestore_error(kind)))
    _assert_retry(client.get("/v1/usage", headers=account[1]))


@pytest.mark.parametrize("kind", ["unavailable", "deadline", "aborted"])
def test_transient_usage_counters_are_retryable(client, account, monkeypatch, kind):
    monkeypatch.setattr("queep.usage.document",
                        lambda *_args, **_kwargs: _Broken(_firestore_error(kind)))
    _assert_retry(client.get("/v1/usage", headers=account[1]))


def test_transient_usage_inventory_is_retryable(client, account, monkeypatch):
    def fail(*_args, **_kwargs):
        raise ServiceUnavailable("busy")

    monkeypatch.setattr("queep.usage.inventory", fail)
    _assert_retry(client.get("/v1/usage", headers=account[1]))


def test_storage_reconciliation_is_not_a_transport_retry(client, account, monkeypatch):
    def reconciling(*_args, **_kwargs):
        raise HTTPException(503, "Armazenamento em atualização. Tente novamente.")

    monkeypatch.setattr("queep.usage.inventory", reconciling)
    response = client.get("/v1/usage", headers=account[1])
    assert response.status_code == 503
    assert response.json()["detail"] == "Armazenamento em atualização. Tente novamente."
    assert "Retry-After" not in response.headers


def test_account_and_usage_reads_succeed(client, account):
    _, headers = account
    status = client.get("/v1/account", headers=headers)
    assert status.status_code == 200
    assert status.json() == {"status": "active", "version": 0, "delete_at": None,
                             "can_restore": False}
    usage = client.get("/v1/usage", headers=headers)
    assert usage.status_code == 200
    assert usage.json()["plan"] == "max"
    assert usage.json()["counters"]["operations"]["used"] == 0


def test_closed_account_keeps_status_and_blocks_usage(client, account):
    _, headers = account
    change = {"operation_id": str(uuid4()), "version": 0}
    scheduled = client.post("/v1/account/deletion", headers=headers, json=change)
    assert scheduled.status_code == 202
    status = client.get("/v1/account", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "scheduled"
    assert status.json()["can_restore"] is True
    usage = client.get("/v1/usage", headers=headers)
    assert usage.status_code == 401
    assert usage.json()["detail"] == "Esta conta foi encerrada"
    assert "Retry-After" not in usage.headers


def test_openapi_account_and_usage_declare_retry(client):
    spec = client.app.openapi()
    for path in ("/v1/account", "/v1/usage"):
        responses = spec["paths"][path]["get"]["responses"]
        for status in ("200", "401", "503"):
            schema = responses[status]["content"]["application/json"]["schema"]
            assert schema["$ref"].startswith("#/components/schemas/")
        assert "Retry-After" in responses["503"]["headers"]
    schemas = spec["components"]["schemas"]
    assert schemas["AccountStatus"]["required"] == ["status", "version", "delete_at", "can_restore"]
    assert "counters" in schemas["UsageSnapshot"]["properties"]
    assert schemas["APIError"]["required"] == ["detail"]


def test_classifier_keeps_rejected_sessions_and_maps_retry_errors():
    assert auth_retry(auth.RevokedIdTokenError("revoked")) is None
    assert auth_retry(auth.InvalidIdTokenError("bad")) is None
    mapped = auth_retry(auth.CertificateFetchError("certs", cause=ConnectionError("down")))
    assert mapped.status_code == 503 and mapped.headers["Retry-After"] == "1"
    exhausted = firestore_retry(RetryError("exhausted", cause=ServiceUnavailable("busy")))
    assert exhausted.status_code == 503 and exhausted.headers["Retry-After"] == "1"
    assert firestore_retry(FailedPrecondition("index")) is None
    assert firestore_retry(ValueError("unrelated")) is None
