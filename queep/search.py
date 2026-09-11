from fastapi import HTTPException
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from queep.ai import SPACE
from queep.models import Reference, SearchResults


def search(repo, ai, uid, query):
    collection = repo.collection(uid)
    has_pending = any(collection.where(filter=FieldFilter("deleted", "==", False)).where(
        filter=FieldFilter("status", "in", ["queued", "processing", "error"])).limit(1).stream())
    ready = (collection.where(filter=FieldFilter("deleted", "==", False))
             .where(filter=FieldFilter("status", "==", "ready"))
             .where(filter=FieldFilter("embedding_space", "==", SPACE)))
    if not any(ready.limit(1).stream()):
        return SearchResults(items=[], has_pending=has_pending)
    ai.budget.rate_limit(uid)
    vector = ai.embed(query, query=True)
    candidates = [Reference.model_validate(doc.to_dict()) for doc in ready.find_nearest(
        vector_field="embedding", query_vector=Vector(vector),
        distance_measure=DistanceMeasure.COSINE, limit=10).stream()]
    # Resolve current revisions both before sending to the model and before returning.
    candidates = current_references(repo, uid, candidates)
    selected = ai.verify(query, candidates)
    by_id = {ref.id: ref for ref in candidates}
    visible = current_references(repo, uid, [by_id[identifier] for identifier in selected])
    return SearchResults(items=visible, has_pending=has_pending)


def current_references(repo, uid, references):
    visible = []
    for previous in references:
        try:
            current = repo.get(uid, previous.id)
            if current.revision == previous.revision and current.status == "ready":
                visible.append(current)
        except HTTPException as error:
            if error.status_code != 404:
                raise
    return visible
