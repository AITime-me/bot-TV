#!/usr/bin/env bash
# Create a scheduled PostgreSQL backup for bot-TV staging (custom-format pg_dump).
# Does not stop api/worker/postgres. Does not touch production.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/staging-ops-common.sh
source "${SCRIPT_DIR}/lib/staging-ops-common.sh"

BACKUP_HELP=0
RETENTION_DAYS="$BOT_TV_SCHEDULED_BACKUP_DEFAULT_RETENTION_DAYS"
BACKUP_PATH=""
BACKUP_MANIFEST=""
PURGED_COUNT=0

usage() {
  cat <<'EOF'
Usage: scripts/ops/staging-backup-db.sh [--dry-run] [--retention-days N] [--help]

Create a scheduled PostgreSQL backup for bot-TV staging (pg_dump -Fc).
Does not stop the app or postgres. Safe for live staging.

Options:
  --dry-run            Validate environment and print plan only
  --retention-days N   Keep scheduled backups for N days (default: 14)
  --help               Show this help

Dump files: <STAGE_ROOT>/backups/postgres/YYYYMMDDTHHMMSSZ_scheduled.dump
Only *_scheduled.dump files are purged by retention.

Requires: docker, flock; run as deploy from a bot-TV checkout.
Environment overrides:
  BOT_TV_STAGE_ROOT, BOT_TV_STAGING_POSTGRES_CONTAINER,
  BOT_TV_STAGING_DB, BOT_TV_STAGING_USER, BOT_TV_STAGING_PASSWORD_FILE,
  BOT_TV_STAGING_BACKUPS_DIR
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)
        OPS_DRY_RUN=1
        ;;
      --retention-days)
        shift
        [[ $# -gt 0 ]] || ops_die "--retention-days requires a value"
        RETENTION_DAYS="$1"
        ;;
      --help|-h)
        BACKUP_HELP=1
        ;;
      *)
        ops_die "unknown argument: $1"
        ;;
    esac
    shift
  done
  if [[ "$BACKUP_HELP" -eq 1 ]]; then
    usage
    exit 0
  fi
  ops_validate_retention_days "$RETENTION_DAYS"
}

check_prerequisites() {
  ops_require_commands docker flock
  ops_check_docker_daemon
  ops_cd_repo_root "$(pwd)"
  ops_assert_backups_outside_git
  if [[ ! -f "$BOT_TV_STAGING_PASSWORD_FILE" ]]; then
    ops_die "password file missing: ${BOT_TV_STAGING_PASSWORD_FILE}"
  fi
}

print_plan() {
  ops_info "=== bot-TV staging database backup plan ==="
  ops_info "  stage root: ${BOT_TV_STAGE_ROOT}"
  ops_info "  postgres container: ${BOT_TV_STAGING_POSTGRES_CONTAINER}"
  ops_info "  database: ${BOT_TV_STAGING_DB}"
  ops_info "  target directory: ${BOT_TV_STAGING_BACKUPS_DIR}/"
  ops_info "  filename pattern: YYYYMMDDTHHMMSSZ_scheduled.dump"
  ops_info "  retention days: ${RETENTION_DAYS} (scheduled files only)"
  ops_info "  app/worker/postgres: not stopped"
  if [[ "$OPS_DRY_RUN" -eq 1 ]]; then
    ops_info "Dry-run — no backup file, lock, or retention changes."
  fi
}

main() {
  parse_args "$@"
  check_prerequisites
  print_plan

  if [[ "$OPS_DRY_RUN" -eq 1 ]]; then
    BACKUP_PATH="${BOT_TV_STAGING_BACKUPS_DIR}/$(date -u +%Y%m%dT%H%M%SZ)_scheduled.dump"
    ops_info "  would create: ${BACKUP_PATH}"
    ops_info "Dry-run complete — no changes were made."
    exit 0
  fi

  ops_acquire_staging_ops_lock
  BACKUP_PATH="$(ops_create_scheduled_postgres_backup)"
  ops_info "Backup created: ${BACKUP_PATH}"

  PURGED_COUNT="$(ops_purge_expired_scheduled_backups "$RETENTION_DAYS")"
  if (( PURGED_COUNT > 0 )); then
    ops_info "Retention: removed ${PURGED_COUNT} expired scheduled backup(s)"
  fi

  BACKUP_MANIFEST="$(ops_write_scheduled_backup_manifest "$BACKUP_PATH" "$RETENTION_DAYS" "$PURGED_COUNT" "success")"
  ops_info "Manifest: ${BACKUP_MANIFEST}"
  ops_info "Scheduled backup complete."
}

main "$@"
