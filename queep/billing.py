"""Server-owned subscription access. Webhooks invalidate; RevenueCat confirms.

No receipt, entitlement, UID or expiry supplied by an app can grant access.
The cache bounds webhook delays to five minutes and never extends an expiry.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import HTTPException
from google.cloud import firestore
from pydantic import BaseModel, Field, computed_field

from queep.account_state import is_closed, require_open
from queep.plans import LIMITS, Plan, denied, features
from queep.repository import Repository


class Access(BaseModel):
    mode: Literal["development", "required"] = "required"
    active: bool
    expires_at: datetime | None = None
    will_renew: bool = False
    checked_at: datetime
    plan: Plan = "free"

    @computed_field
    @property
    def features(self) -> dict[str, bool]:
        return features(self.plan)

    @computed_field
    @property
    def limits(self) -> dict[str, int]:
        return LIMITS[self.plan].copy()


class Event(BaseModel):
    # Extra fields/new event types are intentionally accepted for forward compatibility.
    id: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    app_user_id: str | None = Field(None, max_length=1000)
    original_app_user_id: str | None = Field(None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    transferred_from: list[str] = Field(default_factory=list, max_length=100)
    transferred_to: list[str] = Field(default_factory=list, max_length=100)


class Webhook(BaseModel):
    event: Event


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def unavailable():
    return HTTPException(503, "Não foi possível confirmar a assinatura. Tente novamente.",
                         headers={"Retry-After": "10"})


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Subscription dates must have a timezone")
    return parsed


def interpret_customer(data, entitlement, accept_sandbox, now, max_entitlement="queep_max"):
    for name, plan in ((max_entitlement, "max"), (entitlement, "pro")):
        access = interpret_entitlement(data, name, accept_sandbox, now)
        if access.active:
            return access.model_copy(update={"plan": plan})
    return Access(active=False, checked_at=now)


def interpret_entitlement(data, entitlement, accept_sandbox, now):
    """V1 supports auto-renewing subscriptions, including grace periods."""
    subscriber = data["subscriber"]
    entitlements = subscriber["entitlements"]
    subscriptions = subscriber["subscriptions"]
    if not isinstance(entitlements, dict) or not isinstance(subscriptions, dict):
        raise ValueError("Invalid subscriber")
    item = entitlements.get(entitlement)
    inactive = Access(active=False, checked_at=now)
    if item is None:
        return inactive
    purchase = subscriptions.get(item["product_identifier"])
    # Lifetime and promotional grants are outside this subscription-only launch.
    if not purchase or not item.get("expires_date") or not purchase.get("expires_date"):
        return inactive
    if purchase.get("refunded_at") is not None:
        return inactive
    if purchase.get("is_sandbox") is not False and not accept_sandbox:
        return inactive
    if timestamp(item["purchase_date"]) > now:
        return inactive
    expiry = min(timestamp(item["expires_date"]), timestamp(purchase["expires_date"]))
    grace = purchase.get("grace_period_expires_date")
    if grace:
        expiry = max(expiry, timestamp(grace))
    return Access(active=expiry > now, expires_at=expiry, checked_at=now,
                  will_renew=expiry > now and purchase.get("unsubscribe_detected_at") is None)


class Billing:
    def __init__(self, db, settings, transport=None):
        self.db = db
        self.settings = settings
        self.repo = Repository(db)
        self.transport = transport

    def document(self, uid):
        return self.db.collection("billing_access").document(digest(uid))

    def customer(self, uid, now):
        key = self.settings.revenuecat_key
        if not key:
            raise unavailable()
        try:
            with httpx.Client(timeout=10, follow_redirects=False,
                              transport=self.transport) as client:
                response = client.get(
                    "https://api.revenuecat.com/v1/subscribers/" + quote(uid, safe=""),
                    headers={"Authorization": "Bearer " + key.get_secret_value()},
                )
                response.raise_for_status()
                return interpret_customer(response.json(), self.settings.revenuecat_entitlement,
                                          self.settings.billing_accept_sandbox, datetime.now(UTC),
                                          self.settings.revenuecat_max_entitlement)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
            # Never return vendor payloads, tokens or stale access on an upstream failure.
            raise unavailable() from error

    def access(self, uid, force=False):
        require_open(self.db, uid)
        now = datetime.now(UTC)
        if self.settings.billing_mode == "development":
            return Access(mode="development", active=True, plan="max", checked_at=now)
        doc = self.document(uid)
        token = uuid4().hex

        @firestore.transactional
        def claim(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict() or {}
            if old.get("retry_after", now) > now:
                raise unavailable()
            cached = old.get("access")
            if cached and "plan" in cached and old.get("valid_until", now) > now:
                access = Access.model_validate(cached)
                unexpired = not access.active or (access.expires_at and access.expires_at > now)
                # Client refresh is capped to one provider call per 10s per account.
                if unexpired and (not force or now - access.checked_at < timedelta(seconds=10)):
                    return access, None
            if old.get("lease_until", now) > now:
                raise unavailable()
            tx.set(doc, {"lease": token, "lease_until": now + timedelta(seconds=30)}, merge=True)
            return None, old.get("generation", 0)

        cached, generation = self.repo.transact(claim)
        if cached is not None:
            return cached
        try:
            access = self.customer(uid, now)

            @firestore.transactional
            def publish(tx):
                require_open(self.db, uid, tx)
                current = doc.get(transaction=tx).to_dict() or {}
                if current.get("lease") != token or current.get("generation", 0) != generation:
                    # An event arrived during the provider request. Do not publish that snapshot.
                    raise unavailable()
                ttl = timedelta(seconds=300 if access.active else 60)
                valid_until = now + ttl
                if access.active and access.expires_at:
                    valid_until = min(valid_until, access.expires_at)
                tx.set(doc, {"access": access.model_dump(), "valid_until": valid_until,
                             "lease": firestore.DELETE_FIELD,
                             "lease_until": firestore.DELETE_FIELD}, merge=True)
            self.repo.transact(publish)
            return access
        except HTTPException:
            @firestore.transactional
            def backoff(tx):
                if is_closed(self.db, uid, tx):
                    return
                current = doc.get(transaction=tx).to_dict() or {}
                if current.get("lease") == token:
                    tx.update(doc, {"valid_until": now,
                                    "retry_after": now + timedelta(seconds=10)})
            self.repo.transact(backoff)
            raise
        finally:
            @firestore.transactional
            def release(tx):
                if is_closed(self.db, uid, tx):
                    return
                current = doc.get(transaction=tx).to_dict() or {}
                if current.get("lease") == token:
                    tx.update(doc, {"lease": firestore.DELETE_FIELD,
                                    "lease_until": firestore.DELETE_FIELD})
            self.repo.transact(release)

    def require(self, uid, feature=None):
        access = self.access(uid)
        if feature and not access.features.get(feature, False):
            raise denied(access.plan, feature)
        return access

    def authenticate_webhook(self, authorization):
        token = self.settings.revenuecat_webhook_token
        if not token or len(token.get_secret_value()) < 32:
            raise HTTPException(503, "Integração indisponível")
        if not hmac.compare_digest((authorization or "").encode(),
                                   ("Bearer " + token.get_secret_value()).encode()):
            raise HTTPException(401, "Webhook não autorizado")

    def event(self, event):
        identities = set(event.aliases + event.transferred_from + event.transferred_to)
        identities.update([event.app_user_id, event.original_app_user_id])
        identities.discard(None)
        if any(not uid or len(uid) > 1000 for uid in identities) or len(identities) > 100:
            raise HTTPException(422, "Evento inválido")
        if not identities and event.type != "TEST":
            raise HTTPException(422, "Evento sem conta")
        receipt = self.db.collection("billing_events").document(digest(event.id))
        now = datetime.now(UTC)

        @firestore.transactional
        def invalidate(tx):
            if receipt.get(transaction=tx).exists:
                return
            docs = [self.document(uid) for uid in sorted(identities)
                    if not is_closed(self.db, uid, tx)]
            records = [doc.get(transaction=tx).to_dict() or {} for doc in docs]
            # All affected aliases and both sides of transfers are invalidated atomically.
            # The next access consults RevenueCat instead of applying an out-of-order event.
            for doc, old in zip(docs, records, strict=True):
                tx.set(doc, {"generation": old.get("generation", 0) + 1, "valid_until": now,
                             "lease": firestore.DELETE_FIELD,
                             "retry_after": firestore.DELETE_FIELD,
                             "lease_until": firestore.DELETE_FIELD}, merge=True)
            tx.create(receipt, {"received_at": now, "expires_at": now + timedelta(days=30)})
        self.repo.transact(invalidate)
