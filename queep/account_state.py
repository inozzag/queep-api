"""A permanent, content-free barrier against late writes and billing callbacks."""

import hashlib

from fastapi import HTTPException


def deletion_document(db, uid):
    return db.collection("account_deletions").document(hashlib.sha256(uid.encode()).hexdigest())


def is_closed(db, uid, tx=None):
    return deletion_document(db, uid).get(transaction=tx).exists


def require_open(db, uid, tx=None):
    if is_closed(db, uid, tx):
        raise HTTPException(401, "Esta conta foi encerrada", headers={"WWW-Authenticate": "Bearer"})
