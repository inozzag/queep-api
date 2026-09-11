#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Run after deploy-dev.sh and deploy-ai-dev.sh. No RevenueCat secret is required.
cloud=(--project=queep-dev-hstlx --account=gazzoni@hstlx.com --quiet)
region=southamerica-east1
digest() {
  local revision
  revision=$(gcloud run services describe "$1" "${cloud[@]}" --region="$region" \
    --format='value(status.latestReadyRevisionName)')
  gcloud run revisions describe "$revision" "${cloud[@]}" --region="$region" \
    --format='value(status.imageDigest)'
}
api_digest=$(digest queep-api)
worker_digest=$(digest queep-worker)
test -n "$api_digest"
test "$api_digest" = "$worker_digest"

role=projects/queep-dev-hstlx/roles/queepAccountDeletion
if ! gcloud iam roles describe queepAccountDeletion "${cloud[@]}" >/dev/null 2>&1; then
  gcloud iam roles create queepAccountDeletion "${cloud[@]}" \
    --title='Queep account deletion' --permissions=firebaseauth.users.delete --stage=GA
fi
gcloud projects add-iam-policy-binding queep-dev-hstlx "${cloud[@]}" \
  --member=serviceAccount:queep-api@queep-dev-hstlx.iam.gserviceaccount.com \
  --role="$role" --condition=None --format=none

# Update and test maintenance before the API can create recoverable schedules.
bash infra/deploy-image-cleanup-dev.sh
gcloud run jobs execute queep-image-cleanup "${cloud[@]}" --region="$region" --wait
gcloud run services update queep-api "${cloud[@]}" --region="$region" \
  --update-env-vars=QUEEP_ACCOUNT_DELETION_ENABLED=true,REVENUECAT_PROJECT_ID=projf6e36522
