"""Idempotent, project-scoped development resources. Secrets never enter command arguments."""
import os
import subprocess

PROJECT = "queep-dev-hstlx"
REGION = "southamerica-east1"
CLOUD = [f"--project={PROJECT}", "--account=gazzoni@hstlx.com", "--quiet"]


def run(*args, input=None):
    subprocess.run(["gcloud", *args, *CLOUD], check=True, input=input)


def exists(*args):
    return subprocess.run(["gcloud", *args, *CLOUD], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def main():
    run("services", "enable", "cloudtasks.googleapis.com", "secretmanager.googleapis.com")
    for name in ("queep-ai", "queep-task-invoker"):
        email = f"{name}@{PROJECT}.iam.gserviceaccount.com"
        if not exists("iam", "service-accounts", "describe", email):
            run("iam", "service-accounts", "create", name, f"--display-name={name}")
    worker = f"serviceAccount:queep-ai@{PROJECT}.iam.gserviceaccount.com"
    run("projects", "add-iam-policy-binding", PROJECT, f"--member={worker}",
        "--role=roles/datastore.user", "--condition=None", "--format=none")
    run("storage", "buckets", "add-iam-policy-binding",
        f"gs://{PROJECT}.firebasestorage.app", f"--member={worker}",
        "--role=roles/storage.objectViewer", "--format=none")
    if not exists("secrets", "describe", "queep-openrouter"):
        run("secrets", "create", "queep-openrouter", "--replication-policy=user-managed",
            f"--locations={REGION}", "--labels=app=queep,environment=development")
    if not exists("secrets", "versions", "describe", "latest", "--secret=queep-openrouter"):
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key.startswith("sk-or-"):
            raise SystemExit("Load the private .env.evaluation file; do not paste keys in chat.")
        run("secrets", "versions", "add", "queep-openrouter", "--data-file=-", input=key.encode())
    operation = "update" if exists("tasks", "queues", "describe", "queep-references",
                                   f"--location={REGION}") else "create"
    run("tasks", "queues", operation, "queep-references", f"--location={REGION}",
        "--max-dispatches-per-second=0.2", "--max-concurrent-dispatches=2",
        "--max-attempts=5", "--min-backoff=30s", "--max-backoff=300s")
    for name in ("queep-api", "queep-ai"):
        member = f"serviceAccount:{name}@{PROJECT}.iam.gserviceaccount.com"
        run("secrets", "add-iam-policy-binding", "queep-openrouter", f"--member={member}",
            "--role=roles/secretmanager.secretAccessor", "--format=none")
        run("tasks", "queues", "add-iam-policy-binding", "queep-references",
            f"--location={REGION}", f"--member={member}",
            "--role=roles/cloudtasks.enqueuer", "--format=none")
        run("iam", "service-accounts", "add-iam-policy-binding",
            f"queep-task-invoker@{PROJECT}.iam.gserviceaccount.com",
            f"--member={member}", "--role=roles/iam.serviceAccountUser", "--format=none")


if __name__ == "__main__":
    main()
