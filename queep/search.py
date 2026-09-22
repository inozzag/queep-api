from uuid import uuid4

from fastapi import HTTPException
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from queep.ai import SPACE
from queep.cost_context import attribution
from queep.models import Reference, SearchResults
from queep.usage import policy


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
    if not any(document.to_dict().get("archived_at") is None for document in ready.stream()):
        return SearchResults(items=[], has_pending=has_pending)
    ai.budget.rate_limit(uid)
    vector = ai.embed(query, query=True)
    candidates = [Reference.model_validate(doc.to_dict()) for doc in ready.find_nearest(
        vector_field="embedding", query_vector=Vector(vector),
        distance_measure=DistanceMeasure.COSINE, limit=50).stream()]
    # Resolve current revisions both before sending to the model and before returning.
    candidates = current_references(repo, uid, candidates, space_id)[:10]
    selected = ai.verify(query, candidates)
    by_id = {ref.id: ref for ref in candidates}
    visible = current_references(
        repo, uid, [by_id[identifier] for identifier in selected], space_id)
    return SearchResults(items=visible, has_pending=has_pending)


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
