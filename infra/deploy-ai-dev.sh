#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Run prepare-ai-dev.py and deploy-dev.sh first. Pin one image across API and worker.
cloud=(--project=queep-dev-hstlx --account=gazzoni@hstlx.com)
region=southamerica-east1
worker_url=https://queep-worker-721281571441.southamerica-east1.run.app
identity=queep-task-invoker@queep-dev-hstlx.iam.gserviceaccount.com
revision=$(gcloud run services describe queep-api "${cloud[@]}" --region="$region" \
  --format='value(status.latestReadyRevisionName)')
digest=$(gcloud run revisions describe "$revision" "${cloud[@]}" --region="$region" \
  --format='value(status.imageDigest)')
test -n "$digest"
settings="QUEEP_ENV=production,QUEEP_BILLING_MODE=development,GOOGLE_CLOUD_PROJECT=queep-dev-hstlx,QUEEP_STORAGE_BUCKET=queep-dev-hstlx.firebasestorage.app,QUEEP_AI_ENABLED=true,QUEEP_AI_DAILY_BUDGET_USD=1,QUEEP_WORKER_URL=$worker_url,QUEEP_TASK_IDENTITY=$identity,QUEEP_TASK_QUEUE=queep-references,QUEEP_REGION=$region"
secret_version=$(gcloud secrets versions describe latest --secret=queep-openrouter \
  "${cloud[@]}" --format='value(name.basename())')
test -n "$secret_version"

gcloud run deploy queep-worker "${cloud[@]}" --region="$region" --image="$digest" \
  --command=.venv/bin/uvicorn --args=queep.worker:app,--host,0.0.0.0,--port,8080 \
  --service-account=queep-ai@queep-dev-hstlx.iam.gserviceaccount.com \
  --set-env-vars="$settings" --set-secrets="OPENROUTER_API_KEY=queep-openrouter:$secret_version" \
  --invoker-iam-check --min=0 --max=1 --cpu=1 --memory=1Gi \
  --concurrency=1 --timeout=300 --cpu-throttling --quiet
gcloud run services add-iam-policy-binding queep-worker "${cloud[@]}" --region="$region" \
  --member="serviceAccount:$identity" --role=roles/run.invoker --quiet --format=none

gcloud run services update queep-api "${cloud[@]}" --region="$region" \
  --update-env-vars="$settings" --update-secrets="OPENROUTER_API_KEY=queep-openrouter:$secret_version" \
  --quiet

operation=create
header_flag=--headers
if gcloud scheduler jobs describe queep-ai-dispatch "${cloud[@]}" --location="$region" >/dev/null 2>&1; then
  operation=update
  header_flag=--update-headers
fi
gcloud scheduler jobs "$operation" http queep-ai-dispatch "${cloud[@]}" --location="$region" \
  --schedule='* * * * *' --time-zone=Etc/UTC --uri="$worker_url/tasks/dispatch" \
  --http-method=POST --message-body='{}' "$header_flag=Content-Type=application/json" \
  --oidc-service-account-email="$identity" --oidc-token-audience="$worker_url" \
  --max-retry-attempts=2 --attempt-deadline=120s --quiet
