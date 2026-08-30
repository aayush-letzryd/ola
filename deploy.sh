#!/usr/bin/env bash
# ==============================================================================
# deploy.sh — Google Cloud Infrastructure Deployment Script for LetzRyd Ola
# ==============================================================================
# Sets up Cloud Run Job ('ola-sync-job') and all 3 Cloud Scheduler cron triggers.
# Region: asia-south1 (Mumbai) | Project: letzryd-dev-test
# ==============================================================================

set -euo pipefail

PROJECT_ID="letzryd-dev-test"
REGION="asia-south1"
JOB_NAME="ola-sync-job"
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${JOB_NAME}:latest"
SERVICE_ACCOUNT="925756819101-compute@developer.gserviceaccount.com"

echo "================================================================================"
echo " 1. Building Container Image via Cloud Build"
echo "================================================================================"
gcloud builds submit --tag "${IMAGE_TAG}"

echo "================================================================================"
echo " 2. Updating Cloud Run Job (${JOB_NAME})"
echo "================================================================================"
gcloud run jobs update "${JOB_NAME}" \
  --image="${IMAGE_TAG}" \
  --region="${REGION}" \
  --cpu=2 \
  --memory=2Gi \
  --task-timeout=3600s \
  --max-retries=0 \
  --set-env-vars="HEADLESS=true,TZ=Asia/Kolkata,GCS_BUCKET_NAME=letzryd-ola-raw-statements,DB_HOST=35.200.196.113,DB_PORT=5432,DB_NAME=postgres,DB_USER=postgres"

echo "================================================================================"
echo " 3. Setting Up Cloud Scheduler Triggers"
echo "================================================================================"

# Trigger 1: Daily Rolling Week Sync & Hourly Retry (6:00 AM to 11:00 AM IST)
echo "Creating/Updating 'ola-daily-sync-trigger' (Hourly 6:00 AM - 11:00 AM IST)..."
gcloud scheduler jobs create http ola-daily-sync-trigger \
  --location="${REGION}" \
  --schedule="0 6,7,8,9,10,11 * * *" \
  --time-zone="Asia/Kolkata" \
  --uri="https://${REGION}-run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run" \
  --http-method=POST \
  --oauth-service-account-email="${SERVICE_ACCOUNT}" \
  --description="Ola Daily Rolling Week Sync & Hourly Retries (6:00 to 11:00 AM IST)" || \
gcloud scheduler jobs update http ola-daily-sync-trigger \
  --location="${REGION}" \
  --schedule="0 6,7,8,9,10,11 * * *" \
  --time-zone="Asia/Kolkata" \
  --uri="https://${REGION}-run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run" \
  --http-method=POST \
  --oauth-service-account-email="${SERVICE_ACCOUNT}"

# Trigger 3: Tuesday Audit Reconciliation (Tuesdays at 08:00 AM IST)
echo "Creating/Updating 'ola-tuesday-audit-trigger' (Tuesdays 08:00 AM IST with override)..."
gcloud scheduler jobs create http ola-tuesday-audit-trigger \
  --location="${REGION}" \
  --schedule="0 8 * * 2" \
  --time-zone="Asia/Kolkata" \
  --uri="https://${REGION}-run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run" \
  --http-method=POST \
  --message-body='{"overrides":{"containerOverrides":[{"args":["--tuesday-audit"]}]}}' \
  --headers="Content-Type=application/json" \
  --oauth-service-account-email="${SERVICE_ACCOUNT}" \
  --description="Ola Tuesday 8:00 AM Reconciliation Audit Trigger" || \
gcloud scheduler jobs update http ola-tuesday-audit-trigger \
  --location="${REGION}" \
  --schedule="0 8 * * 2" \
  --time-zone="Asia/Kolkata" \
  --uri="https://${REGION}-run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run" \
  --http-method=POST \
  --message-body='{"overrides":{"containerOverrides":[{"args":["--tuesday-audit"]}]}}' \
  --update-headers="Content-Type=application/json" \
  --oauth-service-account-email="${SERVICE_ACCOUNT}"

echo "================================================================================"
echo " SUCCESS! All Google Cloud Serverless Infrastructure is Deployed and Live!"
echo "================================================================================"
