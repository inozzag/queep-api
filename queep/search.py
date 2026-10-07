import math
from uuid import uuid4

from fastapi import HTTPException
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.ai import SPACE, AIError
from queep.cost_context import attribution
from queep.models import Reference, SearchResults
from queep.usage import policy

# Cosine neighbors considered before the verifier. Archived rows must not take one of these slots.
NEIGHBOR_LIMIT = 50
CANDIDATE_LIMIT = 10


def search(repo, ai, uid, query, space_id=None):
    access = policy(repo.db, uid)
    with attribution(plan=access.plan, kind="search", operation_id=uuid4().hex):
        return search_with_context(repo, ai, uid, query, space_id)


def search_with_context(repo, ai, uid, query, space_id=None):
    collection = repo.collection(uid)
    if space_id:
        from queep.spaces import Spaces
        Spaces(repo.db).require(uid, [space_id])
        collection = collection.where(filter=FieldFilter("space_ids", "array_contains", space_id))
    pending = collection.where(filter=FieldFilter("deleted", "==", False)).where(
        filter=FieldFilter("status", "in", ["queued", "processing", "error"])).stream()
    has_pending = any(document.to_dict().get("archived_at") is None for document in pending)
    ready = (collection.where(filter=FieldFilter("deleted", "==", False))
             .where(filter=FieldFilter("status", "==", "ready"))
             .where(filter=FieldFilter("embedding_space", "==", SPACE)))
    # The vector index still contains archived embeddings. Rank only active rows so an
    # archived neighbor cannot consume a slot ahead of a live one.
    records = [document.to_dict() for document in ready.stream()]
    if not any(record.get("archived_at") is None for record in records):
        return SearchResults(items=[], has_pending=has_pending)
    token = ai.budget.rate_limit(uid)
    try:
        vector = ai.embed(query, query=True)
        neighbors = nearest_active(records, vector)
        # Resolve current revisions both before sending to the model and before returning.
        candidates = current_references(
            repo, uid, [Reference.model_validate(item) for item in neighbors], space_id
        )[:CANDIDATE_LIMIT]
        selected = ai.verify(query, candidates)
        by_id = {ref.id: ref for ref in candidates}
        visible = current_references(
            repo, uid, [by_id[identifier] for identifier in selected], space_id)
        result = SearchResults(items=visible, has_pending=has_pending)
    except AIError:
        release = getattr(ai.budget, "release_search", None)
        if release is not None:
            release(uid, token)
        raise
    commit = getattr(ai.budget, "commit_search", None)
    if commit is not None and token:
        commit(uid, token)
    return result


def nearest_active(records, vector, limit=NEIGHBOR_LIMIT):
    ranked = []
    for data in records:
        if data.get("archived_at") is not None:
            continue
        score = _similarity(vector, _floats(data.get("embedding")))
        if score is None:
            continue
        ranked.append((score, data.get("id") or "", data))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[:limit]]


def _floats(value):
    if value is None:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _similarity(query, embedding):
    if not query or not embedding or len(query) != len(embedding):
        return None
    dot = norm_query = norm_embedding = 0.0
    for left, right in zip(query, embedding, strict=True):
        dot += left * right
        norm_query += left * left
        norm_embedding += right * right
    if norm_query <= 0 or norm_embedding <= 0:
        return None
    return dot / math.sqrt(norm_query * norm_embedding)


def current_references(repo, uid, references, space_id=None):
    visible = []
    for previous in references:
        try:
            current = repo.get(uid, previous.id)
            if (current.revision == previous.revision and current.status == "ready"
                    and current.archived_at is None
                    and (space_id is None or space_id in current.space_ids)):
                visible.append(current)
        except HTTPException as error:
            if error.status_code != 404:
                raise
    return visible
