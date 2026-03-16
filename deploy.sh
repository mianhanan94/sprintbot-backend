#!/usr/bin/env bash
# ============================================================================
# SprintBot — Automated Cloud Deployment Script
# Deploys backend to Google Cloud Run and frontend to Firebase Hosting
# ============================================================================

set -euo pipefail

# ── Configuration ───────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:-sprintbot-488512}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="${CLOUD_RUN_SERVICE:-sprintbot}"
FRONTEND_DIR="${FRONTEND_DIR:-../sprintbot-command-center}"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()   { echo -e "${GREEN}[DEPLOY]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Pre-flight checks ──────────────────────────────────────────────────────
check_prerequisites() {
    log "Running pre-flight checks..."

    command -v gcloud >/dev/null 2>&1 || error "gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install"

    # Verify authentication
    ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null)
    [ -z "$ACCOUNT" ] && error "Not authenticated. Run: gcloud auth login"
    log "Authenticated as: $ACCOUNT"

    # Set project
    gcloud config set project "$PROJECT_ID" --quiet
    log "Project: $PROJECT_ID"
}

# ── Deploy Backend to Cloud Run ─────────────────────────────────────────────
deploy_backend() {
    log "=========================================="
    log "Deploying backend to Cloud Run..."
    log "  Service:  $SERVICE_NAME"
    log "  Region:   $REGION"
    log "  Project:  $PROJECT_ID"
    log "=========================================="

    gcloud run deploy "$SERVICE_NAME" \
        --source . \
        --region "$REGION" \
        --project "$PROJECT_ID" \
        --allow-unauthenticated \
        --quiet

    # Get the service URL
    SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
        --region "$REGION" \
        --project "$PROJECT_ID" \
        --format="value(status.url)")

    log "Backend deployed: $SERVICE_URL"
    echo ""
}

# ── Deploy Frontend to Firebase Hosting ──────────────────────────────────────
deploy_frontend() {
    if [ ! -d "$FRONTEND_DIR" ]; then
        warn "Frontend directory not found at $FRONTEND_DIR — skipping frontend deployment."
        return 0
    fi

    log "=========================================="
    log "Deploying frontend to Firebase Hosting..."
    log "=========================================="

    command -v firebase >/dev/null 2>&1 || error "Firebase CLI not found. Install: npm install -g firebase-tools"

    pushd "$FRONTEND_DIR" > /dev/null

    log "Installing dependencies..."
    npm ci --silent

    log "Building production bundle..."
    npm run build

    log "Deploying to Firebase Hosting..."
    firebase deploy --only hosting

    popd > /dev/null

    log "Frontend deployed: https://devfte.com"
    echo ""
}

# ── Main ────────────────────────────────────────────────────────────────────
main() {
    echo ""
    log "SprintBot Deployment — $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""

    check_prerequisites

    case "${1:-all}" in
        backend)
            deploy_backend
            ;;
        frontend)
            deploy_frontend
            ;;
        all)
            deploy_backend
            deploy_frontend
            ;;
        *)
            echo "Usage: $0 [backend|frontend|all]"
            echo "  backend   — Deploy Cloud Run service only"
            echo "  frontend  — Deploy Firebase Hosting only"
            echo "  all       — Deploy both (default)"
            exit 1
            ;;
    esac

    log "Deployment complete!"
}

main "$@"
