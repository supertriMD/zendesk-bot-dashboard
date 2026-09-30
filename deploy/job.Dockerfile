# Zendesk daily refresh — Cloud Run Job container. Build context = repo root, uploaded through
# .gcloudignore (an allowlist), so .env*, the duckdb and calibration transcripts never leave the laptop.
#   Built by deploy/deploy_cloud_run.sh via deploy/cloudbuild.yaml.
FROM python:3.12-slim

WORKDIR /app
COPY deploy/requirements-job.txt deploy/requirements-job.txt
RUN pip install --no-cache-dir -r deploy/requirements-job.txt

# Only the job's code. No .env: secrets arrive as env vars from Secret Manager, and BigQuery auth is
# the job's service account via the metadata server (db.client() falls back to ADC), so no key file.
COPY config.py db.py pull_conversations.py score_conversations.py classify_backlog.py ./
COPY deploy/cloudrun_entry.sh deploy/cloudrun_entry.sh

# Provenance: the commit this image was built from (deploy script passes it through cloudbuild.yaml).
ARG GIT_SHA=unknown
ENV ZENDESK_JOB_GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.revision=$GIT_SHA

CMD ["bash", "deploy/cloudrun_entry.sh"]
