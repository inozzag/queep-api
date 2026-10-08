import os
from uuid import uuid4

from fastapi import HTTPException
from google.cloud.firestore_v1.base_query import FieldFilter

from queep.ai import SPACE, AIError
from queep.cost_context import attribution
from queep.lexicon import expand_terms
from queep.models import Reference, SearchResults
from queep.ranking import HEAD_LIMIT, RESULT_LIMIT, contradicting_ids, order_ready, vector_order
from queep.usage import policy

# Legacy shortlist size. Archived rows must not take a neighbor slot; the live window is larger.
NEIGHBOR_LIMIT = 50
CANDIDATE_LIMIT = RESULT_LIMIT


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
        return SearchResults(items=[], has_pending=has_pending, candidate_limit=RESULT_LIMIT)
    token = ai.budget.rate_limit(uid)
    try:
        vector = ai.embed(query, query=True)
    except AIError:
        _release(ai, uid, token)
        raise
    ranked = order_ready(records, vector, query, _expansions(ai, query))
    # Resolve current revisions both before the checker and before returning.
    candidates = current_references(
        repo, uid, [Reference.model_validate(item) for item in ranked], space_id)
    head, tail = candidates[:HEAD_LIMIT], candidates[HEAD_LIMIT:]
    checker_failed = False
    try:
        selected = ai.verify(query, head) if head else []
    except AIError:
        # A dead checker must not look like an empty library or spend the search.
        checker_failed = True
        selected = [ref.id for ref in head]
    kept = set(selected)
    if not checker_failed:
        kept -= set(contradicting_ids(query, [ref for ref in head if ref.id in kept]))
    ordered = [ref for ref in head if ref.id in kept] + list(tail)
    visible = current_references(repo, uid, ordered, space_id)
    if checker_failed:
        _release(ai, uid, token)
    else:
        _commit(ai, uid, token)
    return SearchResults(items=visible, has_pending=has_pending, candidate_limit=CANDIDATE_LIMIT)


def _expansions(ai, query):
    """Curated synonyms boost rank. An optional rewrite never removes the curated list."""
    terms = expand_terms(query)
    flag = os.getenv("QUEEP_QUERY_REWRITE", "").lower()
    if flag not in {"1", "true", "yes"}:
        return terms
    rewrite = getattr(ai, "rewrite_query", None)
    if rewrite is None:
        return terms
    try:
        extra = rewrite(query)
    except Exception:
        return terms
    if not isinstance(extra, list):
        return terms
    merged = list(terms)
    for item in extra:
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _release(ai, uid, token):
    release = getattr(ai.budget, "release_search", None)
    if release is not None:
        release(uid, token)


def _commit(ai, uid, token):
    commit = getattr(ai.budget, "commit_search", None)
    if commit is not None and token:
        commit(uid, token)


def nearest_active(records, vector, limit=NEIGHBOR_LIMIT):
    """Active cosine neighbors. Archived rows never occupy a slot."""
    return [item[2] for item in vector_order(records, vector)[:limit]]


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
