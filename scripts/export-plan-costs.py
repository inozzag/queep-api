"""Read-only rolling 30-day provider-cost export; no content or account IDs in output."""
import json
from datetime import UTC, datetime, timedelta

from google.cloud.firestore_v1.base_query import FieldFilter

from queep.config import database
from queep.margin import summarize

if __name__ == "__main__":
    start = datetime.now(UTC) - timedelta(days=30)
    records = database().collection("ai_calls").where(filter=FieldFilter("created_at", ">=", start))
    print(json.dumps(summarize(row.to_dict() for row in records.stream()), indent=2))
