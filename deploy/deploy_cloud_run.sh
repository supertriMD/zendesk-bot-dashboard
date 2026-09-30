#!/usr/bin/env bash
# Deploy the daily Zendesk refresh as a Cloud Run Job + Cloud Scheduler trigger, so it runs every
# morning without the laptop. Idempotent: re-run it to ship a code change.
#
# YOU run this in YOUR gcloud session (it creates secrets, IAM bindings and a job):
#   gcloud auth login
#   ./deploy/deploy_cloud_run.sh
#
# Secrets come from 1Password (the op:// references in .env.op) and are piped straight into
# Secret Manager: never written to disk, never printed. A new secret version is only added when
# the 1Password value differs from the one already stored (compared by SHA-256).
#
# Once the first scheduled run is green, retire the laptop path: do NOT install
# deploy/com.supertri.zendeskbot.daily.plist, or the two would run the same pull twice a day.
#
# Options (env):
#   SKIP_SCHEDULER=1   deploy the job only, no daily trigger (run it by hand to test)
#   RUN_NOW=1          execute the job once straight after deploying and wait for the result
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-supertri-chat-bot}"          # where the zendesk_bot dataset lives
REGION="${REGION:-europe-west1}"                   # EU-adjacent; the dataset is EU
AR="${AR:-zendesk}"
JOB="${JOB:-zendesk-daily}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$AR/zendesk-job:latest"
# The identity the deployed dashboard already uses (projectWriters on zendesk_bot). On Cloud Run it
# runs KEYLESS via the metadata server; no key file goes anywhere near the job.
RUN_SA="${RUN_SA:-zendesk-bot-dashboard@$PROJECT.iam.gserviceaccount.com}"
SCHEDULE="${SCHEDULE:-30 6 * * *}"                 # 06:30, the time the launchd plist used
TZ_="${TZ_:-Europe/London}"
TASK_TIMEOUT="${TASK_TIMEOUT:-3600}"               # a normal day is minutes; headroom for a backlog
SECRETS=(ZENDESK_SUBDOMAIN ZENDESK_EMAIL ZENDESK_API_TOKEN ANTHROPIC_API_KEY)
# AI agents export credentials: mounted only once their op:// references exist in .env.op.
# Until then pull_bot_export.py SKIPS cleanly. All three or none (the script fails on a partial set).
OPTIONAL_SECRETS=(AI_AGENTS_API_KEY AI_AGENTS_BOT_ID AI_AGENTS_ORG_ID)
_opt_found=0
for s in "${OPTIONAL_SECRETS[@]}"; do grep -qE "^$s=op://" .env.op 2>/dev/null && _opt_found=$((_opt_found+1)); done
case "$_opt_found" in
  0) ;;
  3) SECRETS+=("${OPTIONAL_SECRETS[@]}") ;;
  *) echo "FAIL: .env.op has $_opt_found of the 3 AI_AGENTS_* references; add all three or none."; exit 1 ;;
esac

GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
git diff --quiet HEAD -- config.py db.py pull_conversations.py pull_bot_export.py score_conversations.py \
  classify_backlog.py deploy/ .gcloudignore 2>/dev/null || GIT_SHA="$GIT_SHA-dirty"

step(){ echo; echo "── $* ──"; }
have(){ command -v "$1" >/dev/null 2>&1; }

# ── preflight ────────────────────────────────────────────────────────────────
have gcloud || { echo "FAIL: gcloud not installed."; exit 1; }
have op     || { echo "FAIL: 1Password CLI (op) not installed."; exit 1; }
have shasum || { echo "FAIL: shasum not found."; exit 1; }
gcloud auth print-access-token >/dev/null 2>&1 || { echo "FAIL: not authenticated. Run: gcloud auth login"; exit 1; }
op vault list >/dev/null 2>&1 || { echo "FAIL: 1Password CLI is locked. Unlock the 1Password app and re-run."; exit 1; }
[ -f .env.op ] || { echo "FAIL: .env.op not found."; exit 1; }
gcloud config set project "$PROJECT" >/dev/null
echo "project=$PROJECT region=$REGION job=$JOB sa=$RUN_SA image-commit=$GIT_SHA"

ref_for(){   # $1 = var name -> its op:// reference from .env.op (fail loud if missing)
  local r; r="$(grep -E "^$1=op://" .env.op | head -1 | cut -d= -f2-)"
  [ -n "$r" ] || { echo "FAIL: no op:// reference for $1 in .env.op" >&2; exit 1; }
  printf '%s' "$r"
}

# ── 1. APIs ──────────────────────────────────────────────────────────────────
step "1. enable APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com >/dev/null
echo "   ok"

# ── 2. Artifact Registry ─────────────────────────────────────────────────────
step "2. Artifact Registry repo '$AR'"
if gcloud artifacts repositories describe "$AR" --location="$REGION" >/dev/null 2>&1; then
  echo "   exists"
else
  gcloud artifacts repositories create "$AR" --repository-format=docker --location="$REGION" \
    --description="Zendesk daily refresh job images" >/dev/null
  echo "   created"
fi

# ── 3. secrets: 1Password -> Secret Manager (piped, never on disk) ──────────
step "3. secrets (source: 1Password via .env.op)"
for name in "${SECRETS[@]}"; do
  ref="$(ref_for "$name")"
  want="$(op read --no-newline "$ref" | shasum -a 256 | cut -d' ' -f1)"
  [ "$want" != "$(printf '' | shasum -a 256 | cut -d' ' -f1)" ] || { echo "FAIL: $name resolved empty from 1Password"; exit 1; }
  if ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=user-managed --locations="$REGION" >/dev/null
    op read --no-newline "$ref" | gcloud secrets versions add "$name" --data-file=- >/dev/null
    echo "   $name: created"
  else
    have_="$(gcloud secrets versions access latest --secret="$name" 2>/dev/null | shasum -a 256 | cut -d' ' -f1 || true)"
    if [ "$have_" = "$want" ]; then
      echo "   $name: unchanged"
    else
      op read --no-newline "$ref" | gcloud secrets versions add "$name" --data-file=- >/dev/null
      echo "   $name: new version added (1Password value changed)"
    fi
  fi
  gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUN_SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

# ── 4. build ─────────────────────────────────────────────────────────────────
step "4. build image (uploads only the .gcloudignore allowlist)"
gcloud builds submit --config deploy/cloudbuild.yaml \
  --substitutions "_IMAGE=$IMAGE,_GIT_SHA=$GIT_SHA" --region="$REGION" . >/dev/null
echo "   built $IMAGE @ $GIT_SHA"

# ── 5. job ───────────────────────────────────────────────────────────────────
step "5. Cloud Run job '$JOB'"
SECRET_FLAGS="$(for s in "${SECRETS[@]}"; do printf '%s=%s:latest,' "$s" "$s"; done | sed 's/,$//')"
gcloud run jobs deploy "$JOB" --image="$IMAGE" --region="$REGION" --service-account="$RUN_SA" \
  --set-secrets="$SECRET_FLAGS" --set-env-vars="BQ_PROJECT=$PROJECT,ZENDESK_JOB_GIT_SHA=$GIT_SHA" \
  --task-timeout="${TASK_TIMEOUT}s" --max-retries=1 --memory=1Gi --cpu=1 >/dev/null
echo "   deployed"

# ── 6. scheduler ─────────────────────────────────────────────────────────────
if [ "${SKIP_SCHEDULER:-}" = "1" ]; then
  step "6. scheduler: SKIPPED (SKIP_SCHEDULER=1)"
else
  step "6. Cloud Scheduler '$JOB-trigger' ($SCHEDULE $TZ_)"
  gcloud run jobs add-iam-policy-binding "$JOB" --region="$REGION" \
    --member="serviceAccount:$RUN_SA" --role=roles/run.invoker >/dev/null
  URI="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$JOB:run"
  if gcloud scheduler jobs describe "$JOB-trigger" --location="$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$JOB-trigger" --location="$REGION" --schedule="$SCHEDULE" \
      --time-zone="$TZ_" --uri="$URI" --http-method=POST --oauth-service-account-email="$RUN_SA" >/dev/null
    echo "   updated"
  else
    gcloud scheduler jobs create http "$JOB-trigger" --location="$REGION" --schedule="$SCHEDULE" \
      --time-zone="$TZ_" --uri="$URI" --http-method=POST --oauth-service-account-email="$RUN_SA" >/dev/null
    echo "   created"
  fi
fi

# ── 7. optional first run ────────────────────────────────────────────────────
if [ "${RUN_NOW:-}" = "1" ]; then
  step "7. executing once (waits for the result)"
  gcloud run jobs execute "$JOB" --region="$REGION" --wait
fi

step "done"
echo "   logs:  gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=$JOB' --limit=50 --format='value(textPayload)'"
