"""Verify quota exhaustion only on NEW disposable development accounts."""

import json
import plistlib
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from google.cloud import firestore
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "queep-dev-hstlx"
BASE = "https://queep-api-721281571441.southamerica-east1.run.app"


def main():
    config = plistlib.loads(
        (
            ROOT.parent / "queep-apple/Config/Firebase/Development/iOS/GoogleService-Info.plist"
        ).read_bytes()
    )
    assert config["PROJECT_ID"] == PROJECT
    credential = subprocess.check_output(
        [
            "gcloud",
            "auth",
            "print-access-token",
            "--account=gazzoni@hstlx.com",
        ],
        text=True,
    ).strip()
    db = firestore.Client(project=PROJECT, credentials=Credentials(credential))
    users = []
    with httpx.Client(timeout=60) as client:

        def api(user, method, path, body=None, expected=200):
            response = client.request(
                method,
                BASE + path,
                json=body,
                headers={"Authorization": "Bearer " + user["idToken"]},
            )
            assert response.status_code == expected, f"{method} {path}: {response.status_code}"
            assert response.headers["cache-control"] == "no-store"
            return response

        try:
            assert client.get(BASE + "/v1/usage").status_code == 401
            for _ in range(2):
                result = client.post(
                    "https://identitytoolkit.googleapis.com/v1/accounts:signUp",
                    params={"key": config["API_KEY"]},
                    json={
                        "email": f"quota-smoke-{uuid4()}@example.invalid",
                        "password": secrets.token_urlsafe(24),
                        "returnSecureToken": True,
                    },
                )
                assert result.status_code == 200
                users.append(result.json())
            owner, other = users
            path = f"/v1/references/{uuid4()}"
            note = {
                "kind": "note",
                "title": "Synthetic quota check",
                "text": "A disposable note for the Queep operational limits test.",
            }
            api(owner, "PUT", path, note)
            api(owner, "PUT", path, note)
            usage = api(owner, "GET", "/v1/usage").json()
            assert usage["counters"]["operations"]["used"] == 1
            assert api(other, "GET", "/v1/usage").json()["counters"]["operations"]["used"] == 0
            # Simulate exhaustion on the identity just created, without generating 100 AI jobs.
            assert owner["email"].startswith("quota-smoke-")
            day = datetime.now(UTC).date().isoformat()
            ledger = (
                db.collection("users").document(owner["localId"]).collection("usage").document(day)
            )
            cap = usage["counters"]["operations"]["limit"]
            ledger.update({"operations": cap})
            failure = api(owner, "PUT", f"/v1/references/{uuid4()}", note, expected=429)
            assert int(failure.headers["retry-after"]) > 0
            api(owner, "PUT", path, note)  # Existing upload remains retryable at the limit.
            api(owner, "GET", path)
            api(owner, "DELETE", path + "?revision=1", expected=204)
            assert api(owner, "GET", "/v1/usage").json()["counters"]["operations"]["used"] == cap
            print(
                "PASS: usage isolation, exact retry, exhaustion, Retry-After, read/delete at limit."
            )
            report = {
                "project": PROJECT,
                "checked_at": datetime.now(UTC).isoformat(),
                "passed": True,
                "limits": {key: value["limit"] for key, value in usage["counters"].items()},
            }
            (ROOT / "evaluation/results/cloud-usage-smoke.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
        finally:
            for user in users:
                state = api(user, "GET", "/v1/account").json()
                api(
                    user,
                    "POST",
                    "/v1/account/deletion",
                    {
                        "version": state["version"],
                        "operation_id": str(uuid4()),
                        "immediate": True,
                    },
                    expected=202,
                )
            print(
                "Disposable accounts submitted for immediate erasure; no existing account selected."
            )


if __name__ == "__main__":
    main()
