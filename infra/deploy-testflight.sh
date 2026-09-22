#!/usr/bin/env bash
# GAZ-115: Apple sandbox in the existing Firebase project, with isolated billing services.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" != --deploy ]]; then
  cat <<'PLAN'
Usage: bash infra/deploy-testflight.sh --deploy

Destination: queep-dev-hstlx / southamerica-east1 / gazzoni@hstlx.com
Creates or updates only the TestFlight API, worker, task queue and dispatcher.
Billing: required; Apple sandbox accepted; outbox: jobs_testflight.
Uses existing runtime/build identities and OpenRouter secret.
Creates a dedicated webhook secret; never prints its value.
The shared development API, worker and queue are not updated.
Requires a valid gcloud login and the public iOS SDK key in BillingTestFlight.xcconfig.
PLAN
  exit 0
fi

cloud=(--project=queep-dev-hstlx --account=gazzoni@hstlx.com --quiet)
region=southamerica-east1
api=queep-api-testflight
worker=queep-worker-testflight
queue=queep-references-testflight
dispatcher=queep-testflight-dispatch
webhook_secret=queep-testflight-revenuecat-webhook
api_identity=queep-api@queep-dev-hstlx.iam.gserviceaccount.com
worker_identity=queep-ai@queep-dev-hstlx.iam.gserviceaccount.com
invoker=queep-task-invoker@queep-dev-hstlx.iam.gserviceaccount.com

# Fail before any mutation if login or public SDK configuration is missing.
gcloud auth print-access-token --account=gazzoni@hstlx.com >/dev/null
sdk_key=$(python3 - <<'PY'
import re
from pathlib import Path
path = Path('../queep-apple/Config/BillingTestFlight.xcconfig')
match = re.search(r'^QUEEP_REVENUECAT_IOS_KEY\s*=\s*(appl_[A-Za-z0-9]+)\s*$',
                  path.read_text(), re.MULTILINE)
if not match:
    raise SystemExit('A public Apple SDK key is required; Test Store is forbidden.')
print(match.group(1))
PY
)
openrouter_version=$(gcloud secrets versions describe latest --secret=queep-openrouter \
  "${cloud[@]}" --format='value(name.basename())')
test -n "$openrouter_version"

if ! gcloud secrets describe "$webhook_secret" "${cloud[@]}" >/dev/null 2>&1; then
  gcloud secrets create "$webhook_secret" "${cloud[@]}" \
    --replication-policy=user-managed --locations="$region" \
    --labels=app=queep,environment=testflight
fi
if ! gcloud secrets versions describe latest --secret="$webhook_secret" \
    "${cloud[@]}" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | \
    gcloud secrets versions add "$webhook_secret" "${cloud[@]}" --data-file=-
fi
webhook_version=$(gcloud secrets versions describe latest --secret="$webhook_secret" \
  "${cloud[@]}" --format='value(name.basename())')
test -n "$webhook_version"
gcloud secrets add-iam-policy-binding "$webhook_secret" "${cloud[@]}" \
  --member="serviceAccount:$api_identity" --role=roles/secretmanager.secretAccessor --format=none

operation=create
if gcloud tasks queues describe "$queue" "${cloud[@]}" \
    --location="$region" >/dev/null 2>&1; then operation=update; fi
gcloud tasks queues "$operation" "$queue" "${cloud[@]}" --location="$region" \
  --max-dispatches-per-second=0.2 --max-concurrent-dispatches=2 \
  --max-attempts=5 --min-backoff=30s --max-backoff=300s
for identity in "$api_identity" "$worker_identity"; do
  gcloud tasks queues add-iam-policy-binding "$queue" "${cloud[@]}" --location="$region" \
    --member="serviceAccount:$identity" --role=roles/cloudtasks.enqueuer --format=none
done

settings="QUEEP_ENV=production,QUEEP_BILLING_MODE=required,QUEEP_BILLING_ACCEPT_SANDBOX=true"
settings+=",GOOGLE_CLOUD_PROJECT=queep-dev-hstlx,QUEEP_STORAGE_BUCKET=queep-dev-hstlx.firebasestorage.app"
settings+=",QUEEP_AI_ENABLED=true,QUEEP_AI_DAILY_BUDGET_USD=1,QUEEP_REGION=$region"
settings+=",QUEEP_TASK_QUEUE=$queue,QUEEP_JOB_COLLECTION=jobs_testflight,QUEEP_TASK_IDENTITY=$invoker"
settings+=",REVENUECAT_API_KEY=$sdk_key,REVENUECAT_ENTITLEMENT=queep_pro,REVENUECAT_MAX_ENTITLEMENT=queep_max"
settings+=",REVENUECAT_PROJECT_ID=projf6e36522,QUEEP_ACCOUNT_DELETION_ENABLED=true"

# Bootstrap privately, then use exactly the same image in both services.
gcloud run deploy "$api" "${cloud[@]}" --region="$region" --source=. \
  --build-service-account=projects/queep-dev-hstlx/serviceAccounts/queep-build@queep-dev-hstlx.iam.gserviceaccount.com \
  --service-account="$api_identity" --set-env-vars="$settings" \
  --set-secrets="OPENROUTER_API_KEY=queep-openrouter:$openrouter_version,REVENUECAT_WEBHOOK_TOKEN=$webhook_secret:$webhook_version" \
  --invoker-iam-check --min=0 --max=2 --cpu=1 --memory=1Gi \
  --concurrency=8 --timeout=120 --cpu-throttling
revision=$(gcloud run services describe "$api" "${cloud[@]}" --region="$region" \
  --format='value(status.latestReadyRevisionName)')
digest=$(gcloud run revisions describe "$revision" "${cloud[@]}" --region="$region" \
  --format='value(status.imageDigest)')
test -n "$digest"
gcloud run deploy "$worker" "${cloud[@]}" --region="$region" --image="$digest" \
  --command=.venv/bin/uvicorn --args=queep.worker:app,--host,0.0.0.0,--port,8080 \
  --service-account="$worker_identity" --set-env-vars="$settings" \
  --set-secrets="OPENROUTER_API_KEY=queep-openrouter:$openrouter_version" \
  --invoker-iam-check --min=0 --max=1 --cpu=1 --memory=1Gi \
  --concurrency=1 --timeout=300 --cpu-throttling
worker_url=$(gcloud run services describe "$worker" "${cloud[@]}" --region="$region" \
  --format='value(status.url)')
test -n "$worker_url"
gcloud run services update "$worker" "${cloud[@]}" --region="$region" \
  --update-env-vars="QUEEP_WORKER_URL=$worker_url"
gcloud run services add-iam-policy-binding "$worker" "${cloud[@]}" --region="$region" \
  --member="serviceAccount:$invoker" --role=roles/run.invoker --format=none

operation=create
header_flag=--headers
if gcloud scheduler jobs describe "$dispatcher" "${cloud[@]}" \
    --location="$region" >/dev/null 2>&1; then
  operation=update
  header_flag=--update-headers
fi
gcloud scheduler jobs "$operation" http "$dispatcher" "${cloud[@]}" --location="$region" \
  --schedule='* * * * *' --time-zone=Etc/UTC --uri="$worker_url/tasks/dispatch" \
  --http-method=POST --message-body='{}' "$header_flag=Content-Type=application/json" \
  --oidc-service-account-email="$invoker" --oidc-token-audience="$worker_url" \
  --max-retry-attempts=2 --attempt-deadline=120s

# The API edge is reachable by Apple apps; application routes still enforce Firebase.
# The worker keeps both IAM and verified task-identity checks.
gcloud run services update "$api" "${cloud[@]}" --region="$region" \
  --update-env-vars="QUEEP_WORKER_URL=$worker_url" --no-invoker-iam-check
gcloud run services describe "$api" "${cloud[@]}" --region="$region" \
  --format='value(status.url)'
