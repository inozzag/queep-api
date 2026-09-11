import json
import logging
from functools import lru_cache

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from queep.config import Settings, database
from queep.jobs import Jobs
from queep.repository import Repository

logger = logging.getLogger(__name__)


@lru_cache
def task_client():
    return tasks_v2.CloudTasksClient()


def enqueue(task, settings, jobs, client=None):
    if not settings.worker_url or not settings.task_identity:
        return False
    client = client or task_client()
    parent = client.queue_path(settings.project, settings.region, settings.queue)
    name = f"{task['job_id']}-{task['run_id']}-r{task['revision']}-a{task['attempt']}"
    try:
        client.create_task(parent=parent, task={
            "name": f"{parent}/tasks/{name}", "dispatch_deadline": {"seconds": 120},
            "http_request": {"http_method": tasks_v2.HttpMethod.POST,
                             "url": settings.worker_url + "/tasks/process",
                             "headers": {"Content-Type": "application/json"},
                             "body": json.dumps(task).encode(),
                             "oidc_token": {"service_account_email": settings.task_identity,
                                            "audience": settings.worker_url}}},
                           timeout=3, retry=None)
    except AlreadyExists:
        pass
    jobs.enqueued(task)
    return True


def dispatch_one(uid, ref_id):
    settings = Settings()
    if not settings.ai_enabled or settings.environment == "local":
        return
    try:
        db = database()
        jobs = Jobs(db)
        doc = Repository(db).job(uid, ref_id).get()
        if doc.exists and "run_id" in doc.to_dict():
            enqueue(jobs.payload(doc), settings, jobs)
    except Exception:
        # Saving already succeeded atomically. The scheduled dispatcher recovers the outbox.
        logger.warning("ai_dispatch_deferred")
