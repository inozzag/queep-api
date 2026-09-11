"""Account lifecycle barrier shared by the API and asynchronous writers."""

import hashlib

from fastapi import HTTPException


def deletion_document(db, uid):
    return db.collection("account_deletions").document(hashlib.sha256(uid.encode()).hexdigest())


def is_closed(db, uid, tx=None):
    record = deletion_document(db, uid).get(transaction=tx).to_dict()
    return bool(record and record.get("state") != "active")


def require_open(db, uid, tx=None):
    if is_closed(db, uid, tx):
        raise HTTPException(401, "Esta conta foi encerrada", headers={"WWW-Authenticate": "Bearer"})


def account_version(db, uid, tx=None):
    record = deletion_document(db, uid).get(transaction=tx).to_dict() or {}
    return record.get("version", 0)
