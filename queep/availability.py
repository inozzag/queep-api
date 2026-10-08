"""Retryable dependency failures share the same 503 contract as contended writes."""

from fastapi import HTTPException
from firebase_admin import auth, exceptions
from google.api_core.exceptions import Aborted, DeadlineExceeded, RetryError, ServiceUnavailable
from google.auth.exceptions import TransportError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

BUSY = "Serviço ocupado. Tente novamente."
RETRY_AFTER = "1"

# Identity Toolkit and certificate fetch failures. Revoked or invalid tokens are not in this set.
AUTH_TRANSIENT = (
    auth.CertificateFetchError,
    exceptions.UnknownError,
    exceptions.UnavailableError,
    exceptions.DeadlineExceededError,
    exceptions.InternalError,
    exceptions.AbortedError,
    exceptions.ResourceExhaustedError,
    TransportError,
    RequestsConnectionError,
    Timeout,
)
FIRESTORE_TRANSIENT = (Aborted, DeadlineExceeded, ServiceUnavailable)


def retry_later():
    return HTTPException(503, BUSY, headers={"Retry-After": RETRY_AFTER})


def auth_retry(error):
    if isinstance(error, AUTH_TRANSIENT):
        return retry_later()
    return None


def firestore_retry(error):
    if isinstance(error, FIRESTORE_TRANSIENT):
        return retry_later()
    if isinstance(error, ValueError) and isinstance(error.__cause__, Aborted):
        return retry_later()
    cause = getattr(error, "cause", None)
    if isinstance(error, RetryError) and isinstance(cause, FIRESTORE_TRANSIENT):
        return retry_later()
    return None


def call_read(operation):
    try:
        return operation()
    except Exception as error:
        mapped = firestore_retry(error)
        if mapped is not None:
            raise mapped from error
        raise
