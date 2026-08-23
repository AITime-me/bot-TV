#!/usr/bin/env bash
# Shared helpers for bot-TV staging PostgreSQL backup / ops.
# Never source .env wholesale; never print passwords or DATABASE_URL values.

if [[ -n "${BOT_TV_STAGING_OPS_COMMON_LOADED:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
BOT_TV_STAGING_OPS_COMMON_LOADED=1

# Server defaults (override via env for harness / non-canonical layouts).
readonly BOT_TV_STAGE_ROOT_DEFAULT="/srv/automation-data/bot-tv/stage"
readonly BOT_TV_STAGING_POSTGRES_CONTAINER_DEFAULT="tv_bot_stage-postgres-1"
readonly BOT_TV_STAGING_DB_DEFAULT="bot_tv_stage"
readonly BOT_TV_STAGING_USER_DEFAULT="bot_tv_stage"
readonly BOT_TV_SCHEDULED_BACKUP_DEFAULT_RETENTION_DAYS=14
readonly BOT_TV_SCHEDULED_BACKUP_NAME_RE='^[0-9]{8}T[0-9]{6}Z_scheduled\.dump$'
readonly BOT_TV_DUMP_NAME_RE='^[0-9]{8}T[0-9]{6}Z_[A-Za-z0-9._-]+\.dump$'

BOT_TV_STAGE_ROOT="${BOT_TV_STAGE_ROOT:-$BOT_TV_STAGE_ROOT_DEFAULT}"
BOT_TV_STAGING_POSTGRES_CONTAINER="${BOT_TV_STAGING_POSTGRES_CONTAINER:-$BOT_TV_STAGING_POSTGRES_CONTAINER_DEFAULT}"
BOT_TV_STAGING_DB="${BOT_TV_STAGING_DB:-$BOT_TV_STAGING_DB_DEFAULT}"
BOT_TV_STAGING_USER="${BOT_TV_STAGING_USER:-$BOT_TV_STAGING_USER_DEFAULT}"
BOT_TV_STAGING_PASSWORD_FILE="${BOT_TV_STAGING_PASSWORD_FILE:-${BOT_TV_STAGE_ROOT}/postgres/postgres_password}"
BOT_TV_STAGING_BACKUPS_DIR="${BOT_TV_STAGING_BACKUPS_DIR:-${BOT_TV_STAGE_ROOT}/backups/postgres}"
BOT_TV_STAGING_OPS_LOCK="${BOT_TV_STAGING_OPS_LOCK:-${BOT_TV_STAGE_ROOT}/backups/deploy-state/.staging-ops.lock}"

OPS_DRY_RUN=0
OPS_REPO_ROOT=""

ops_die() {
  echo "error: $*" >&2
  exit 1
}

ops_info() {
  echo "$*"
}

ops_require_commands() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || ops_die "required command not found: ${cmd}"
  done
}

ops_find_repo_root() {
  local start_dir="${1:-$(pwd)}"
  local dir="$start_dir"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "${dir}/.git" && -f "${dir}/docker-compose.yml" ]]; then
      OPS_REPO_ROOT="$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  ops_die "not inside bot-TV git repository (started from ${start_dir})"
}

ops_cd_repo_root() {
  ops_find_repo_root "${1:-$(pwd)}"
  cd "$OPS_REPO_ROOT" || ops_die "cannot cd to repository root"
}

ops_check_docker_daemon() {
  if ! docker info >/dev/null 2>&1; then
    ops_die "docker daemon is not available"
  fi
}

ops_container_exists() {
  docker inspect "$1" >/dev/null 2>&1
}

ops_container_running() {
  local running
  running="$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null || echo false)"
  [[ "$running" == "true" ]]
}

ops_container_healthy() {
  local health
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo missing)"
  case "$health" in
    healthy|none) return 0 ;;
    *) return 1 ;;
  esac
}

ops_ensure_private_dir() {
  local dir="$1"
  mkdir -p "$dir" || ops_die "cannot create directory: ${dir}"
  chmod 700 "$dir" 2>/dev/null || true
}

ops_validate_retention_days() {
  local value="$1"
  if [[ -z "$value" || ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    ops_die "--retention-days must be a positive integer"
  fi
  if (( value > 3650 )); then
    ops_die "--retention-days exceeds safe maximum (3650)"
  fi
}

ops_acquire_staging_ops_lock() {
  local lock_dir
  lock_dir="$(dirname -- "$BOT_TV_STAGING_OPS_LOCK")"
  ops_ensure_private_dir "$lock_dir"
  exec 9>"$BOT_TV_STAGING_OPS_LOCK"
  if ! flock -n 9; then
    ops_die "another staging database operation is already in progress"
  fi
}

ops_read_password_file() {
  local path="$1"
  local value
  [[ -f "$path" && ! -L "$path" ]] || ops_die "postgres password file missing or is a symlink"
  [[ -r "$path" ]] || ops_die "postgres password file is unreadable"
  value="$(tr -d '\r\n' <"$path")"
  [[ -n "$value" ]] || ops_die "postgres password file is empty"
  # Never echo; caller assigns to env for docker exec only.
  printf '%s' "$value"
}

ops_assert_backups_outside_git() {
  # Dump directory must not live inside the git worktree.
  local resolved_backups resolved_repo
  if [[ -d "$OPS_REPO_ROOT" ]]; then
    resolved_repo="$(realpath -e -- "$OPS_REPO_ROOT" 2>/dev/null || readlink -f -- "$OPS_REPO_ROOT" 2>/dev/null || true)"
    resolved_backups="$(realpath -m -- "$BOT_TV_STAGING_BACKUPS_DIR" 2>/dev/null || echo "$BOT_TV_STAGING_BACKUPS_DIR")"
    if [[ -n "$resolved_repo" && "$resolved_backups" == "$resolved_repo"/* ]]; then
      ops_die "backup directory must not be inside the git worktree"
    fi
  fi
}

ops_verify_pg_dump_file() {
  local backup_path="$1"
  local remote_path="/tmp/bot-tv-dump-verify-$$.dump"
  local status=0

  [[ -f "$backup_path" && -s "$backup_path" ]] || return 1
  docker cp "$backup_path" "${BOT_TV_STAGING_POSTGRES_CONTAINER}:${remote_path}" >/dev/null || return 1
  set +e
  docker exec "$BOT_TV_STAGING_POSTGRES_CONTAINER" pg_restore -l "$remote_path" >/dev/null 2>&1
  status=$?
  set -e
  docker exec "$BOT_TV_STAGING_POSTGRES_CONTAINER" rm -f -- "$remote_path" >/dev/null 2>&1 || true
  return "$status"
}

ops_create_scheduled_postgres_backup() {
  local timestamp_utc backup_name backup_path tmp_path password

  timestamp_utc="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_name="${timestamp_utc}_scheduled.dump"
  backup_path="${BOT_TV_STAGING_BACKUPS_DIR}/${backup_name}"
  tmp_path="${backup_path}.tmp.$$"

  ops_ensure_private_dir "$BOT_TV_STAGING_BACKUPS_DIR"
  ops_assert_backups_outside_git

  if [[ -e "$backup_path" ]]; then
    ops_die "backup already exists: ${backup_path}"
  fi

  if ! ops_container_exists "$BOT_TV_STAGING_POSTGRES_CONTAINER"; then
    ops_die "postgres container does not exist: ${BOT_TV_STAGING_POSTGRES_CONTAINER}"
  fi
  if ! ops_container_running "$BOT_TV_STAGING_POSTGRES_CONTAINER"; then
    ops_die "postgres container is not running: ${BOT_TV_STAGING_POSTGRES_CONTAINER}"
  fi
  if ! ops_container_healthy "$BOT_TV_STAGING_POSTGRES_CONTAINER"; then
    ops_die "postgres container is not healthy: ${BOT_TV_STAGING_POSTGRES_CONTAINER}"
  fi

  password="$(ops_read_password_file "$BOT_TV_STAGING_PASSWORD_FILE")"

  # Stream custom-format dump to host; never log password.
  set +e
  docker exec -e "PGPASSWORD=${password}" "$BOT_TV_STAGING_POSTGRES_CONTAINER" \
    pg_dump -U "$BOT_TV_STAGING_USER" -d "$BOT_TV_STAGING_DB" -Fc \
    >"$tmp_path"
  local dump_rc=$?
  set -e
  password=""
  unset password

  if [[ "$dump_rc" -ne 0 ]]; then
    rm -f -- "$tmp_path" 2>/dev/null || true
    ops_die "pg_dump failed (exit ${dump_rc})"
  fi

  chmod 600 "$tmp_path" || true
  if [[ ! -s "$tmp_path" ]]; then
    rm -f -- "$tmp_path" 2>/dev/null || true
    ops_die "backup file is empty"
  fi

  if ! ops_verify_pg_dump_file "$tmp_path"; then
    rm -f -- "$tmp_path" 2>/dev/null || true
    ops_die "backup failed pg_restore -l verification"
  fi

  mv -f -- "$tmp_path" "$backup_path"
  chmod 600 "$backup_path" || true
  printf '%s' "$backup_path"
}

ops_purge_expired_scheduled_backups() {
  local retention_days="$1"
  local cutoff now purged=0 f base epoch
  now="$(date +%s)"
  cutoff=$((now - retention_days * 86400))
  shopt -s nullglob
  for f in "${BOT_TV_STAGING_BACKUPS_DIR}"/*_scheduled.dump; do
    [[ -f "$f" && ! -L "$f" ]] || continue
    base="$(basename -- "$f")"
    [[ "$base" =~ $BOT_TV_SCHEDULED_BACKUP_NAME_RE ]] || continue
    epoch="$(stat -c '%Y' "$f" 2>/dev/null || echo 0)"
    if [[ "$epoch" =~ ^[0-9]+$ ]] && (( epoch < cutoff )); then
      rm -f -- "$f" && purged=$((purged + 1)) || true
    fi
  done
  shopt -u nullglob
  printf '%s' "$purged"
}

ops_write_scheduled_backup_manifest() {
  local backup_path="$1"
  local retention_days="$2"
  local purged_count="$3"
  local status="$4"
  local manifest sha size
  manifest="${backup_path}.manifest.env"
  size="$(stat -c '%s' "$backup_path" 2>/dev/null || echo 0)"
  if command -v sha256sum >/dev/null 2>&1; then
    sha="$(sha256sum -- "$backup_path" | awk '{print $1}')"
  else
    sha="$(shasum -a 256 -- "$backup_path" | awk '{print $1}')"
  fi
  umask 0077
  cat >"$manifest" <<EOF
SCHEMA_VERSION=1
STATUS=${status}
BACKUP_BASENAME=$(basename -- "$backup_path")
BACKUP_SIZE_BYTES=${size}
BACKUP_SHA256=${sha}
RETENTION_DAYS=${retention_days}
PURGED_SCHEDULED_COUNT=${purged_count}
POSTGRES_CONTAINER=${BOT_TV_STAGING_POSTGRES_CONTAINER}
POSTGRES_DB=${BOT_TV_STAGING_DB}
CREATED_AT_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  chmod 600 "$manifest" || true
  printf '%s' "$manifest"
}
