"""Enable Queep development erasure after validating the limited vendor key.

Run from queep-api with `uv run python infra/enable-account-deletion-dev.py`.
Only synthetic RevenueCat customers are used for validation; no purchases.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ["--project=queep-dev-hstlx", "--account=gazzoni@hstlx.com", "--quiet"]
REGION = "--region=southamerica-east1"
IDENTITY = "serviceAccount:queep-api@queep-dev-hstlx.iam.gserviceaccount.com"
SECRET = "queep-revenuecat-deletion"
PROJECT = "projf6e36522"


def cloud(*args):
    return subprocess.check_output(["gcloud", *args, *CLOUD], text=True).strip()


def validate_vendor(key):
    fixture = f"queep-erasure-check-{uuid4()}"
    base = f"https://api.revenuecat.com/v2/projects/{PROJECT}/customers"
    with httpx.Client(headers={"Authorization": "Bearer " + key}, timeout=20) as client:
        response = client.post(base, json={"id": fixture})
        if response.status_code != 201:
            raise SystemExit(f"RevenueCat permission check failed: HTTP {response.status_code}")
        print(f"Disposable vendor fixture: {fixture}", flush=True)
        response = client.delete(f"{base}/{fixture}")
        if response.status_code not in (200, 404):
            raise SystemExit(f"RevenueCat deletion failed: HTTP {response.status_code}")
        # Acceptance is asynchronous. Never enable based only on DELETE's 200.
        for _ in range(20):
            response = client.get(f"{base}/{fixture}")
            if response.status_code == 404:
                print("RevenueCat read/write/deletion confirmed.", flush=True)
                return
            if response.status_code != 200:
                raise SystemExit(f"RevenueCat confirmation failed: HTTP {response.status_code}")
            time.sleep(2)
    raise SystemExit("Vendor deletion still pending; check the fixture before retrying.")


def main():
    settings = dotenv_values(ROOT / ".env.accounts")
    key = settings.get("REVENUECAT_DELETE_KEY", "")
    if settings.get("REVENUECAT_PROJECT_ID") != PROJECT or not key.startswith("sk_"):
        raise SystemExit("Run scripts/configure-revenuecat-deletion.py first.")
    # Worker must have the same guards as the API before accepting account closure.
    digests = []
    for service in ("queep-api", "queep-worker"):
        revision = cloud("run", "services", "describe", service, REGION,
                         "--format=value(status.latestReadyRevisionName)")
        digests.append(cloud("run", "revisions", "describe", revision, REGION,
                             "--format=value(status.imageDigest)"))
    if not digests[0] or digests[0] != digests[1]:
        raise SystemExit("Deploy the current API and worker together first.")
    validate_vendor(key)
    role = "projects/queep-dev-hstlx/roles/queepAccountDeletion"
    existing = cloud("iam", "roles", "list", "--filter=name:" + role, "--format=value(name)")
    if not existing:
        cloud("iam", "roles", "create", "queepAccountDeletion", "--title=Queep account deletion",
              "--permissions=firebaseauth.users.delete", "--stage=GA")
    cloud("projects", "add-iam-policy-binding", "queep-dev-hstlx", "--member=" + IDENTITY,
          "--role=" + role, "--condition=None", "--format=none")
    names = cloud("secrets", "list", "--filter=name:" + SECRET, "--format=value(name)")
    if not names:
        cloud("secrets", "create", SECRET, "--replication-policy=automatic")
    # Secrets never appear in argv, logs or the repository.
    with tempfile.NamedTemporaryFile(mode="w", prefix="queep-vendor-") as stream:
        os.chmod(stream.name, 0o600)
        stream.write(key)
        stream.flush()
        version = cloud("secrets", "versions", "add", SECRET, "--data-file=" + stream.name,
                        "--format=value(name)").split("/")[-1]
    cloud("secrets", "add-iam-policy-binding", SECRET, "--member=" + IDENTITY,
          "--role=roles/secretmanager.secretAccessor", "--format=none")
    subprocess.run(["bash", str(ROOT / "infra/deploy-image-cleanup-dev.sh")], check=True)
    binding = f"--update-secrets=REVENUECAT_DELETE_KEY={SECRET}:{version}"
    environment = (f"--update-env-vars=REVENUECAT_PROJECT_ID={PROJECT},"
                   "QUEEP_BILLING_MODE=development")
    cloud("run", "jobs", "update", "queep-image-cleanup", REGION, binding, environment)
    # Verify maintenance startup before exposing the irreversible request endpoint.
    cloud("run", "jobs", "execute", "queep-image-cleanup", REGION, "--wait")
    cloud("run", "services", "update", "queep-api", REGION, binding,
          environment + ",QUEEP_ACCOUNT_DELETION_ENABLED=true")
    print("Account deletion enabled in Queep development; billing remains development.")


if __name__ == "__main__":
    main()
