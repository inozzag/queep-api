"""Persistent spending reservations; failed/unknown calls keep their allowance."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from google.cloud import firestore

from queep.account_state import require_open
from queep.cost_context import current
from queep.plans import denied, month_window
from queep.repository import Repository
from queep.usage import daily_document, document, exceeded, policy, window


class Budget:
    ALLOWANCE = .01

    def __init__(self, db, daily_limit, search_daily_limit=100):
        self.db = db
        self.limit = daily_limit
        self.search_daily_limit = search_daily_limit
        self.repo = Repository(db)

    def reserve(self):
        now = datetime.now(UTC)
        day = self.db.collection("ai_budget").document(now.date().isoformat())
        ticket = self.db.collection("ai_calls").document(uuid4().hex)

        @firestore.transactional
        def write(tx):
            total = (day.get(transaction=tx).to_dict() or {}).get("used", 0.0)
            if total + self.ALLOWANCE > self.limit + 1e-9:
                raise HTTPException(429, "A IA atingiu o limite de uso de hoje. Tente amanhã.")
            tx.set(day, {"used": total + self.ALLOWANCE, "updated_at": now})
            tx.set(ticket, {"day": day.id, "state": "reserved", "amount": self.ALLOWANCE,
                            "created_at": now, **(current.get() or {}),
                            "expires_at": now + timedelta(days=90)})
        self.repo.transact(write)
        return ticket

    def settle(self, ticket, cost, metadata):
        @firestore.transactional
        def write(tx):
            old = ticket.get(transaction=tx).to_dict()
            if not old or old["state"] != "reserved":
                return
            day = self.db.collection("ai_budget").document(old["day"])
            total = day.get(transaction=tx).to_dict()["used"]
            amount = old["amount"] if cost is None else cost
            tx.update(day, {"used": max(0.0, total - old["amount"] + amount)})
            tx.update(ticket, {"state": "settled", "amount": amount,
                               "cost_known": cost is not None, "cost_usd": cost,
                               **metadata})
        self.repo.transact(write)

    def rate_limit(self, uid):
        """Reserve one search before paid calls. The caller commits or releases the token."""
        access = policy(self.db, uid)
        now, reset = month_window()
        doc = document(self.db, uid, now)
        daily = daily_document(self.db, uid, now)
        token = uuid4().hex

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict() or {}
            abuse = daily.get(transaction=tx).to_dict() or {}
            last = old.get("last_search")
            if old.get("searches", 0) >= access.limits["searches"]:
                raise denied(access.plan, "searches", now=now)
            if abuse.get("searches", 0) >= self.search_daily_limit:
                raise exceeded("Limite diário de buscas atingido. Consulte Uso e privacidade.",
                               window(now)[1], now)
            if last and (now-last).total_seconds() < 1:
                raise HTTPException(429, "Aguarde um instante antes de buscar novamente.",
                                    headers={"Retry-After": "1"})
            pending = [item for item in old.get("pending_searches") or []
                       if isinstance(item, dict) and item.get("id")]
            pending.append({"id": token, "at": now})
            tx.set(doc, {"searches": old.get("searches", 0) + 1, "last_search": now,
                         "pending_searches": pending,
                         "expires_at": reset + timedelta(days=90)}, merge=True)
            tx.set(daily, {"searches": abuse.get("searches", 0) + 1,
                           "expires_at": window(now)[1] + timedelta(days=30)}, merge=True)
        self.repo.transact(write)
        return token

    def commit_search(self, uid, token):
        """Keep a reserved search after verification succeeds."""
        self._settle_search(uid, token, keep=True)

    def release_search(self, uid, token):
        """Return a reserved search when verification does not finish."""
        self._settle_search(uid, token, keep=False)

    def _settle_search(self, uid, token, *, keep):
        if not token:
            return
        now, reset = month_window()
        doc = document(self.db, uid, now)
        daily = daily_document(self.db, uid, now)

        @firestore.transactional
        def write(tx):
            old = doc.get(transaction=tx).to_dict() or {}
            abuse = daily.get(transaction=tx).to_dict() or {}
            pending = [item for item in old.get("pending_searches") or []
                       if isinstance(item, dict) and item.get("id")]
            if not any(item["id"] == token for item in pending):
                return
            pending = [item for item in pending if item["id"] != token]
            changes = {"pending_searches": pending,
                       "expires_at": reset + timedelta(days=90)}
            if not keep:
                changes["searches"] = max(0, old.get("searches", 0) - 1)
            tx.set(doc, changes, merge=True)
            if not keep and abuse.get("searches", 0) > 0:
                tx.set(daily, {"searches": abuse["searches"] - 1,
                               "expires_at": window(now)[1] + timedelta(days=30)}, merge=True)
        self.repo.transact(write)
