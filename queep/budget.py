"""Persistent spending reservations; failed/unknown calls keep their allowance."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from google.cloud import firestore

from queep.account_state import require_open
from queep.repository import Repository


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
                            "expires_at": now + timedelta(days=30)})
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
            tx.update(ticket, {"state": "settled", "amount": amount, "cost_known": cost is not None,
                               **metadata})
        self.repo.transact(write)

    def rate_limit(self, uid):
        """Per-account search limit, checked before paid calls."""
        now = datetime.now(UTC)
        doc = self.db.collection("users").document(uid).collection("usage").document(
            now.date().isoformat())

        @firestore.transactional
        def write(tx):
            require_open(self.db, uid, tx)
            old = doc.get(transaction=tx).to_dict() or {}
            last = old.get("last_search")
            if (old.get("searches", 0) >= self.search_daily_limit
                    or (last and (now-last).total_seconds() < 1)):
                raise HTTPException(429, "Limite de buscas atingido. Tente mais tarde.")
            tx.set(doc, {"searches": old.get("searches", 0) + 1, "last_search": now})
        self.repo.transact(write)
