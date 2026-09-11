#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:---private}" in
  --private) access=(--invoker-iam-check) ;;
  --public) access=(--no-invoker-iam-check) ;;
  *) echo "Usage: bash infra/deploy-dev.sh [--private|--public]" >&2; exit 2 ;;
esac

# Explicit development destination: never inherit another account or project.
gcloud run deploy queep-api \
  --project=queep-dev-hstlx --account=gazzoni@hstlx.com \
  --region=southamerica-east1 --source=. \
  --build-service-account=projects/queep-dev-hstlx/serviceAccounts/queep-build@queep-dev-hstlx.iam.gserviceaccount.com \
  --service-account=queep-api@queep-dev-hstlx.iam.gserviceaccount.com \
  --set-env-vars=QUEEP_ENV=production,GOOGLE_CLOUD_PROJECT=queep-dev-hstlx,QUEEP_STORAGE_BUCKET=queep-dev-hstlx.firebasestorage.app \
  "${access[@]}" --min=0 --max=2 --cpu=1 --memory=1Gi \
  --concurrency=8 --timeout=120 --cpu-throttling --quiet

# --public requires the project-level organization policy exception. It makes
# the edge reachable by Apple apps; reference endpoints still require Firebase.
