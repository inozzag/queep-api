"""Test recovery/erasure using NEW disposable accounts in Queep development only.

The maintenance clock is advanced ONLY on these fixtures, after uploads finish.
No existing account or library is selected by this script.
"""

import hashlib
import json
import os
import plistlib
import secrets
import subprocess
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
from google.cloud import firestore, storage
from google.oauth2.credentials import Credentials
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "queep-dev-hstlx"
BASE = "https://queep-api-721281571441.southamerica-east1.run.app"


def main():
    config = plistlib.loads((ROOT.parent / "queep-apple/Config/Firebase/Development/iOS/"
                            "GoogleService-Info.plist").read_bytes())
    assert config["PROJECT_ID"] == PROJECT
    token = subprocess.check_output([
        "gcloud", "auth", "print-access-token", "--account=gazzoni@hstlx.com",
    ], text=True).strip()
    db = firestore.Client(project=PROJECT, credentials=Credentials(token))
    bucket = storage.Client(project=PROJECT, credentials=Credentials(token)).bucket(
        PROJECT + ".firebasestorage.app")
    client = httpx.Client(timeout=60)
    users = []
    report = ROOT / ".local/account-recovery-smoke.json"
    report.parent.mkdir(exist_ok=True)

    def checkpoint(status):
        fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"status": status, "project": PROJECT,
                       "fixtures": [{"uid": u["localId"], "email": u["email"]} for u in users]},
                      output, indent=2)

    def authentication(action, body, expected=200):
        response = client.post("https://identitytoolkit.googleapis.com/v1/accounts:" + action,
                               params={"key": config["API_KEY"]}, json=body)
        assert response.status_code == expected, f"Auth {action}: HTTP {response.status_code}"
        return response.json()

    def api(user, method, path, *, body=None, data=None, expected=200):
        response = client.request(method, BASE + path, json=body, content=data,
                                  headers={"Authorization": "Bearer " + user["idToken"]})
        assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}"
        return response

    def change(user, version, immediate=False, cancel=False):
        return api(user, "POST", "/v1/account/deletion" + ("/cancel" if cancel else ""),
                   body={"version": version, "operation_id": str(uuid4()), "immediate": immediate},
                   expected=200 if cancel else 202).json()

    for _ in range(3):
        user = authentication("signUp", {"email": f"recovery-smoke-{uuid4()}@example.invalid",
                                         "password": secrets.token_urlsafe(24),
                                         "returnSecureToken": True})
        users.append(user)
        checkpoint("running")
    owner, observer, immediate_user = users
    note_id, image_id = str(uuid4()), str(uuid4())
    note = {"kind": "note", "title": "Recovery fixture", "text": "Synthetic cloud recovery check"}
    api(owner, "PUT", f"/v1/references/{note_id}", body=note)
    api(observer, "PUT", f"/v1/references/{note_id}", body={**note, "title": "Other account"})
    output = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(output, "PNG")
    picture = api(owner, "PUT", f"/v1/references/{image_id}/image", data=output.getvalue()).json()
    stored = api(owner, "GET", f"/v1/references/{image_id}/image").content
    state = change(owner, 0)
    assert state["status"] == "scheduled" and state["can_restore"]
    deadline = datetime.fromisoformat(state["delete_at"])
    assert timedelta(days=29, hours=23) < deadline - datetime.now(UTC) <= timedelta(days=30)
    api(owner, "GET", "/v1/references", expected=401)
    assert api(owner, "GET", "/v1/account").json() == state
    authentication("lookup", {"idToken": owner["idToken"]})
    assert api(observer, "GET", f"/v1/references/{note_id}").json()["title"] == "Other account"
    active = change(owner, state["version"], cancel=True)
    assert active["status"] == "active"
    assert api(owner, "GET", f"/v1/references/{note_id}").json()["text"] == note["text"]
    assert api(owner, "GET", f"/v1/references/{image_id}/image").content == stored
    state = change(owner, active["version"])
    assert change(immediate_user, 0, immediate=True)["status"] == "deleting"

    # Wait for the initial immediate-deletion pass before changing its test clock.
    # Otherwise that pass can legitimately schedule a later retry after our update.
    immediate_doc = db.collection("account_deletions").document(
        hashlib.sha256(immediate_user["localId"].encode()).hexdigest())
    for _ in range(50):
        record = immediate_doc.get().to_dict()
        if record["retry_after"] > record["requested_at"]:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("Initial erasure pass did not finish in time")

    # Simulate day 30 only for the newly created, already uploaded fixtures.
    past = datetime.now(UTC) - timedelta(seconds=1)
    for user in (owner, immediate_user):
        assert user["email"].startswith("recovery-smoke-")
        doc = db.collection("account_deletions").document(
            hashlib.sha256(user["localId"].encode()).hexdigest())
        record = doc.get().to_dict()
        assert record["uid"] == user["localId"]
        assert record["state"] in ("scheduled", "deleting")
        doc.update({"delete_at": past, "settle_at": past, "retry_after": past})
    assert not api(owner, "GET", "/v1/account").json()["can_restore"]
    api(owner, "POST", "/v1/account/deletion/cancel", expected=409,
        body={"version": state["version"], "operation_id": str(uuid4())})
    subprocess.run([
        "gcloud", "run", "jobs", "execute", "queep-image-cleanup", "--wait", "--quiet",
        "--project=" + PROJECT, "--account=gazzoni@hstlx.com", "--region=southamerica-east1",
    ], check=True, stdout=subprocess.DEVNULL)
    for user in (owner, immediate_user):
        uid = user["localId"]
        key = hashlib.sha256(uid.encode()).hexdigest()
        assert db.collection("account_deletions").document(key).get().to_dict()[
            "state"] == "deleted"
        assert not list(db.collection("users").document(uid).collection("references").stream())
        assert db.collection("provider_deletions").document(key).get().to_dict()[
            "state"] == "pending"
        authentication("lookup", {"idToken": user["idToken"]}, expected=400)
        # This HTTP-only fixture never creates a RevenueCat customer.
        db.collection("provider_deletions").document(key).delete()
        api(user, "GET", "/v1/references", expected=401)
    object_key = hashlib.sha256(f"{owner['localId']}/{image_id}".encode()).hexdigest()
    prefix = f"images/v1/{object_key}/{picture['image']['sha256']}"
    assert not bucket.blob(prefix + "/original").exists()
    assert not bucket.blob(prefix + "/thumbnail").exists()
    assert api(observer, "GET", f"/v1/references/{note_id}").json()["title"] == "Other account"
    api(observer, "DELETE", f"/v1/references/{note_id}?revision=1", expected=204)
    authentication("delete", {"idToken": observer["idToken"]})
    checkpoint("passed; only synthetic tombstones remain")
    print("PASS: 30-day schedule, blocked access, retained Auth/data, explicit recovery, "
          "deadline, immediate deletion, maintenance, private storage and isolation.")


if __name__ == "__main__":
    main()
