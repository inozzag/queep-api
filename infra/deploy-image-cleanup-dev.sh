#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Uses exactly the image already verified/deployed to the development API.
cloud=(--project=queep-dev-hstlx --account=gazzoni@hstlx.com)
region=southamerica-east1
image_digest=$(gcloud run services describe queep-api "${cloud[@]}" --region="$region" \
  --format='value(status.latestReadyRevisionName)')
image_digest=$(gcloud run revisions describe "$image_digest" "${cloud[@]}" --region="$region" \
  --format='value(status.imageDigest)')
test -n "$image_digest"

gcloud run jobs deploy queep-image-cleanup "${cloud[@]}" --region="$region" \
  --image="$image_digest" --command=.venv/bin/python --args=-m,queep.cleanup_images \
  --service-account=queep-api@queep-dev-hstlx.iam.gserviceaccount.com \
  --set-env-vars=QUEEP_ENV=production,GOOGLE_CLOUD_PROJECT=queep-dev-hstlx,QUEEP_STORAGE_BUCKET=queep-dev-hstlx.firebasestorage.app \
  --tasks=1 --parallelism=1 --max-retries=1 --task-timeout=300s --cpu=1 --memory=512Mi --quiet

gcloud run jobs add-iam-policy-binding queep-image-cleanup "${cloud[@]}" --region="$region" \
  --member=serviceAccount:queep-image-scheduler@queep-dev-hstlx.iam.gserviceaccount.com \
  --role=roles/run.invoker --quiet

operation=create
if gcloud scheduler jobs describe queep-image-cleanup "${cloud[@]}" --location="$region" >/dev/null 2>&1; then
  operation=update
fi
gcloud scheduler jobs "$operation" http queep-image-cleanup "${cloud[@]}" --location="$region" \
  --schedule='*/30 * * * *' --time-zone=Etc/UTC \
  --uri=https://run.googleapis.com/v2/projects/queep-dev-hstlx/locations/southamerica-east1/jobs/queep-image-cleanup:run \
  --http-method=POST --message-body='{}' --headers=Content-Type=application/json \
  --oauth-service-account-email=queep-image-scheduler@queep-dev-hstlx.iam.gserviceaccount.com \
  --max-retry-attempts=2 --quiet
