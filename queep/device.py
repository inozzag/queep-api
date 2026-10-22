"""Text the Apple apps read on the device before an upload (Vision OCR, Speech, colors).

The app sends the reading first and the file second, so the worker always finds it.
A reading whose upload never arrives expires through the ``expires_at`` TTL.
"""

import logging
from datetime import UTC, datetime, timedelta

from queep.models import DeviceReading

logger = logging.getLogger(__name__)
COLLECTION = "device_readings"
TTL = timedelta(days=2)


def _doc(db, uid, ref_id):
    return db.collection("users").document(uid).collection(COLLECTION).document(ref_id)


def store(db, uid, ref_id, reading: DeviceReading):
    now = datetime.now(UTC)
    _doc(db, uid, ref_id).set({**reading.model_dump(), "created_at": now,
                               "expires_at": now + TTL})


def load(db, uid, ref_id):
    """The stored reading, or None. A bad record never blocks cloud processing."""
    try:
        record = _doc(db, uid, ref_id).get().to_dict()
        if not record:
            return None
        return DeviceReading.model_validate(
            {key: record[key] for key in DeviceReading.model_fields if key in record})
    except Exception:
        logger.warning("device_reading_unreadable")
        return None
