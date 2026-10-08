"""Account lifecycle barrier shared by the API and asynchronous writers."""

import hashlib

from fastapi import HTTPException

from queep.availability import call_read


def deletion_document(db, uid):
    return db.collection("account_deletions").document(hashlib.sha256(uid.encode()).hexdigest())


def is_closed(db, uid, tx=None):
    record = deletion_document(db, uid).get(transaction=tx).to_dict()
    return bool(record and record.get("state") != "active")


def require_open(db, uid, tx=None):
    # Inside a transaction the writer already maps contention. A bare read must do it here.
    closed = is_closed(db, uid, tx) if tx is not None else call_read(lambda: is_closed(db, uid))
    if closed:
        raise HTTPException(401, "Esta conta foi encerrada", headers={"WWW-Authenticate": "Bearer"})


def account_version(db, uid, tx=None):
    record = deletion_document(db, uid).get(transaction=tx).to_dict() or {}
    return record.get("version", 0)
