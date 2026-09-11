"""Exercise the deployed API with disposable Firebase users; never print tokens."""

import argparse
import json
import plistlib
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4


def request(method, url, body=None, token=None, expected=200, gateway_token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if gateway_token:
        headers["X-Serverless-Authorization"] = f"Bearer {gateway_token}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            status, data = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, data = error.code, error.read()
    if status != expected:
        path = urllib.parse.urlsplit(url).path
        raise AssertionError(f"{method} {path}: {status}, expected {expected}")
    return json.loads(data) if data else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-plist", type=Path, required=True)
    parser.add_argument("--via-iam", action="store_true",
                        help="Use the authorized developer's IAM identity for a private API")
    args = parser.parse_args()
    config = plistlib.loads(args.firebase_plist.read_bytes())
    expected_api = "https://queep-api-721281571441.southamerica-east1.run.app"
    if config["PROJECT_ID"] != "queep-dev-hstlx" or args.api_url.rstrip("/") != expected_api:
        raise SystemExit("This smoke test is restricted to Queep development over HTTPS")
    base = args.api_url.rstrip("/")
    gateway_token = None
    if args.via_iam:
        gateway_token = subprocess.check_output([
            "gcloud", "auth", "print-identity-token", "--account=gazzoni@hstlx.com",
        ], text=True).strip()

    def api(method, url, body=None, token=None, expected=200):
        assert url.startswith(base + "/")
        return request(method, url, body, token, expected, gateway_token)
    auth_base = "https://identitytoolkit.googleapis.com/v1/accounts:"

    def authenticate(action, body):
        return request("POST", f"{auth_base}{action}?key={config['API_KEY']}", body)

    users = []
    reference = None
    try:
        assert api("GET", f"{base}/health") == {"status": "ok"}
        api("GET", f"{base}/v1/references", expected=401)
        api("GET", f"{base}/v1/references", token="invalid", expected=401)
        for _ in range(2):
            credentials = {"email": f"queep-smoke-{uuid4()}@example.invalid",
                           "password": secrets.token_urlsafe(24), "returnSecureToken": True}
            user = authenticate("signUp", credentials)
            users.append((user, credentials))
        owner, credentials = users[0]
        token = owner["idToken"]
        second_session = authenticate("signInWithPassword", credentials)["idToken"]
        ref_id = str(uuid4())
        url = f"{base}/v1/references/{ref_id}"
        capture = {"kind": "note", "title": "Validação de nuvem",
                   "text": "CRM branco e notas sincronizadas."}
        reference = api("PUT", url, capture, token)
        assert api("PUT", url, capture, token) == reference
        assert api("GET", url, token=second_session) == reference
        assert api("GET", f"{base}/v1/references", token=token)["items"][0] == reference
        api("GET", url, token=users[1][0]["idToken"], expected=404)
        api("PUT", url, {**capture, "text": "Reenvio diferente"}, token, expected=409)
        direct = (f"https://firestore.googleapis.com/v1/projects/{config['PROJECT_ID']}"
                  f"/databases/(default)/documents/users/{owner['localId']}/references/{ref_id}")
        request("GET", direct, token=token, expected=403)
        edit = {**capture, "text": "Editado e sincronizado.", "revision": reference["revision"],
                "mutation_id": str(uuid4())}
        reference = api("PATCH", url, edit, token)
        assert api("PATCH", url, edit, token) == reference
        assert api("GET", url, token=second_session)["text"] == edit["text"]
        api("PATCH", url, {**edit, "mutation_id": str(uuid4())}, token, expected=409)
        api("DELETE", f"{url}?revision={reference['revision']}", token=token, expected=204)
        reference = None
        api("GET", url, token=second_session, expected=404)
        api("PUT", url, capture, token, expected=409)
        print("PASS: cloud login, two sessions, capture/retry, isolation, "
              "private rules, edit/conflict, deletion.")
    finally:
        cleanup_errors = []
        if reference is not None:
            try:
                api("DELETE", f"{url}?revision={reference['revision']}",
                    token=users[0][0]["idToken"], expected=204)
            except Exception as error:
                cleanup_errors.append(str(error))
        for user, _ in users:
            try:
                authenticate("delete", {"idToken": user["idToken"]})
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            raise RuntimeError(f"Disposable fixture cleanup needs attention: {cleanup_errors}")
        print("Disposable Auth users and reference contents removed.")


if __name__ == "__main__":
    main()
