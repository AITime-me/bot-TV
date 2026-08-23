#!/usr/bin/env bash
# Isolated PostgreSQL restore-test for existing backup dumps.
# Never attaches to staging DB containers, networks, or volumes.
#
# Lifecycle (single finalizer on EXIT):
#   ERR/INT/TERM only record a pending failure code; they do NOT write evidence.
#   EXIT runs finalize_once: disarm traps → cleanup → verify absent → evidence → exit.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/isolated-restore-test-common.sh
source "${SCRIPT_DIR}/lib/isolated-restore-test-common.sh"

IRT_HELP=0
IRT_DRY_RUN=0
IRT_MODE="run" # run | emergency-cleanup | reap-orphans
IRT_ENV_ARG=""
IRT_DUMP_ARG=""
IRT_CIDFILE_ARG=""
IRT_RUN_ID_ARG=""
IRT_MIGRATION_PROOF=0
IRT_TARGET_REV_ARG=""

IRT_STARTED_EPOCH=0
IRT_FINISHED_EPOCH=0
IRT_RUN_ID=""
IRT_TEMP_CONTAINER=""
IRT_TEMP_CID=""
IRT_TEMP_PASSWORD=""
IRT_DUMP_PATH=""
IRT_DUMP_BASENAME=""
IRT_DUMP_SIZE=0
IRT_DUMP_SHA=""
IRT_DUMP_MTIME_UTC=""
IRT_DUMP_AGE_HOURS=0
IRT_DUMP_DEV=""
IRT_DUMP_INODE=""
IRT_SNAPSHOT_PATH=""
IRT_SNAPSHOT_SHA=""
IRT_RUN_DIR=""
IRT_CIDFILE=""
IRT_CURRENT_MARKER=""
IRT_USER_SCHEMA_COUNT=0
IRT_USER_TABLE_COUNT=0
IRT_STATUS="pending"
IRT_ERROR_CODE=""
IRT_CLEANUP_OK=0
IRT_TEMP_ABSENT=0
IRT_SNAPSHOT_ABSENT=1
IRT_INTEGRITY_OK=0
IRT_PHASE=""
IRT_EXIT_CODE=0
# Relative path under env evidence (e.g. history/pg_restore_<RUN_ID>.error.log); never multiline.
IRT_PG_RESTORE_ERROR_LOG=""
# Cap diagnostic capture so evidence cannot grow without bound.
IRT_PG_RESTORE_DIAG_MAX_BYTES=16384
IRT_WORK_OK=0
IRT_FINALIZED=0
IRT_LOCK_HELD=0
IRT_FORBIDDEN_PRE=""
IRT_DOCKER_RM_RC=""
IRT_SKIP_EVIDENCE=0
IRT_DUMP_MTIME_EPOCH=0
IRT_WAIT_PID=""
# Set only by parent INT/TERM trap — never inferred from child exit status alone.
IRT_SIGNAL_RECEIVED=0
IRT_PROOF_SOURCE_DIR=""
IRT_PROOF_IMAGE=""
IRT_PROOF_20260806="not_run"
IRT_PROOF_20260807="not_run"
IRT_PROOF_20260819="not_run"
IRT_PROOF_FINAL_STATUS="not_run"

usage() {
  cat <<'EOF'
Usage:
  isolated-restore-test.sh --environment staging [options]
  isolated-restore-test.sh --emergency-cleanup --environment ENV [--cidfile PATH] [--run-id ID]
  isolated-restore-test.sh --reap-orphans --environment ENV

Restore the newest (or specified) PostgreSQL dump into a fully isolated temporary
Docker PostgreSQL container, verify integrity aggregates, write evidence, and clean up.

Options:
  --environment ENV     staging (required; bot-TV restore-test is staging-only)
  --dump PATH           Optional dump under the environment backup directory
  --evidence-root DIR   Override evidence root (harness/tests only)
  --dry-run             Validate inputs and print plan only
  --emergency-cleanup   Post-stop / SIGKILL recovery via cidfile + labels
  --reap-orphans        Remove only labeled stopped orphans older than TTL
  --cidfile PATH        cidfile for emergency-cleanup
  --run-id ID           Expected run-id label for emergency-cleanup
  --help                Show help

Isolation guarantees:
  - --network none, --pull=never, private dump snapshot mounted read-only
  - no published ports; memory/cpu/pids limits
  - no attachment to staging networks or volumes
  - working DB containers are never stopped or restarted
  - credentials and row data are never logged

Exit codes:
  0  success (restore + cleanup + evidence)
  10 dump missing / unreadable / stale / invalid / TOCTOU
  20 docker / image / start failure
  30 pg_restore failure
  40 integrity check failure
  50 cleanup / interrupt / emergency incomplete
  60 concurrent lock
  70 usage / configuration
  80 forbidden container metadata changed during run
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --environment)
        shift
        [[ $# -gt 0 ]] || irt_die "--environment requires a value"
        IRT_ENV_ARG="$1"
        ;;
      --dump)
        shift
        [[ $# -gt 0 ]] || irt_die "--dump requires a path"
        IRT_DUMP_ARG="$1"
        ;;
      --evidence-root)
        shift
        [[ $# -gt 0 ]] || irt_die "--evidence-root requires a path"
        IRT_EVIDENCE_ROOT="$1"
        ;;
      --dry-run)
        IRT_DRY_RUN=1
        ;;
      --emergency-cleanup)
        IRT_MODE="emergency-cleanup"
        ;;
      --reap-orphans)
        IRT_MODE="reap-orphans"
        ;;
      --cidfile)
        shift
        [[ $# -gt 0 ]] || irt_die "--cidfile requires a path"
        IRT_CIDFILE_ARG="$1"
        ;;
      --run-id)
        shift
        [[ $# -gt 0 ]] || irt_die "--run-id requires a value"
        IRT_RUN_ID_ARG="$1"
        ;;
      --help|-h)
        IRT_HELP=1
        ;;
      *)
        irt_die "unknown argument: $1"
        ;;
    esac
    shift
  done
}

# --- signals / finalizer ----------------------------------------------------

record_failure() {
  local code="$1"
  local err="$2"
  # Preserve the first failure reason for unexpected paths.
  if [[ "$IRT_EXIT_CODE" -eq 0 ]]; then
    IRT_EXIT_CODE="$code"
  fi
  if [[ -z "$IRT_ERROR_CODE" ]]; then
    IRT_ERROR_CODE="$err"
  fi
  IRT_STATUS="failed"
  IRT_PHASE="${IRT_PHASE:-unknown}"
}

fail() {
  local code="$1"
  local err="$2"
  # Intentional fail always wins over a prior ERR trap from a captured nonzero.
  IRT_EXIT_CODE="$code"
  IRT_ERROR_CODE="$err"
  IRT_STATUS="failed"
  IRT_PHASE="${IRT_PHASE:-unknown}"
  irt_info "ISOLATED_RESTORE_TEST FAIL env=${IRT_ENV:-unknown} code=${IRT_ERROR_CODE} phase=${IRT_PHASE}"
  exit "$IRT_EXIT_CODE"
}

on_err() {
  local ec=$?
  # Do not write evidence here — EXIT finalizer owns that.
  if [[ "$IRT_EXIT_CODE" -eq 0 ]]; then
    IRT_EXIT_CODE="$ec"
  fi
  if [[ -z "$IRT_ERROR_CODE" ]]; then
    IRT_ERROR_CODE="UNEXPECTED_ERROR"
  fi
  IRT_STATUS="failed"
}

on_signal() {
  # Sticky parent-signal flag: finalizer must never promote this run to success.
  IRT_SIGNAL_RECEIVED=1
  # Convert to predictable operational failure; cleanup+evidence happen on EXIT.
  record_failure 50 "INTERRUPTED"
  # Unblock interruptible waits and stop hanging helpers (e.g. docker exec → sleep).
  if [[ -n "${IRT_WAIT_PID:-}" ]]; then
    kill -TERM "${IRT_WAIT_PID}" 2>/dev/null || true
    if command -v pkill >/dev/null 2>&1; then
      pkill -TERM -P "${IRT_WAIT_PID}" 2>/dev/null || true
    fi
  fi
  local jp
  for jp in $(jobs -p 2>/dev/null); do
    kill -TERM "$jp" 2>/dev/null || true
  done
  exit 50
}

disarm_traps() {
  trap - EXIT ERR INT TERM
}

# Run a command in the background and wait in an interruptible way.
# Foreground external commands defer trapped SIGTERM until they exit; `wait`
# returns immediately (>128) so on_signal/EXIT can run (bash manual).
# Child exit >128 without parent INT/TERM is returned to the caller — do NOT
# label it INTERRUPTED (e.g. OOM 137 inside a docker helper).
irt_interruptible_run() {
  local st pid
  "$@" &
  pid=$!
  IRT_WAIT_PID="$pid"
  wait "$pid"
  st=$?
  IRT_WAIT_PID=""
  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
    record_failure 50 "INTERRUPTED"
    exit 50
  fi
  return "$st"
}

# Same as irt_interruptible_run but captures stdout to a file (integrity queries).
irt_interruptible_capture() {
  local dest="$1"
  shift
  local st pid
  "$@" >"$dest" &
  pid=$!
  IRT_WAIT_PID="$pid"
  wait "$pid"
  st=$?
  IRT_WAIT_PID=""
  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
    record_failure 50 "INTERRUPTED"
    exit 50
  fi
  return "$st"
}

# Capture merged stdout+stderr (pg_restore diagnostics). Same interrupt contract.
irt_interruptible_capture_merged() {
  local dest="$1"
  shift
  local st pid
  "$@" >"$dest" 2>&1 &
  pid=$!
  IRT_WAIT_PID="$pid"
  wait "$pid"
  st=$?
  IRT_WAIT_PID=""
  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
    record_failure 50 "INTERRUPTED"
    exit 50
  fi
  return "$st"
}

# Redact secret-like tokens from pg_restore output. Never interpolates live passwords
# into the sed pattern (avoid leaking via argv//proc).
irt_sanitize_pg_restore_diag() {
  # Redact KEY=value pairs that look like secrets. Longer password tokens first.
  sed -E \
    -e 's/([A-Za-z_][A-Za-z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN)|DATABASE_URL|PGPASSWORD)=[^[:space:]]+/\1=<redacted>/gi' \
    -e 's/(password|passwd|pwd)=[^[:space:]]+/\1=<redacted>/gi' \
    -e 's#postgres(ql)?://[^[:space:]]+#postgres://<redacted>#gi'
}

# Publish bounded, sanitized pg_restore diagnostic next to history; link via RUN_ID.
# Best-effort: failure here must not mask the original PG_RESTORE_FAILED.
# Final published bytes (history + active) are hard-capped at max_bytes including trailer.
irt_publish_pg_restore_diagnostic() {
  local src="$1"
  local max_bytes="${IRT_PG_RESTORE_DIAG_MAX_BYTES:-16384}"
  local rel dest latest tmp size trailer trailer_bytes content_budget truncated latest_tmp

  IRT_PG_RESTORE_ERROR_LOG=""
  [[ -n "${IRT_RUN_ID:-}" ]] || return 1
  [[ "$IRT_RUN_ID" =~ $IRT_RUN_ID_RE ]] || return 1
  [[ -f "$src" ]] || return 1
  if ! irt_ensure_evidence_dirs; then
    return 1
  fi
  if [[ ! "$max_bytes" =~ ^[1-9][0-9]*$ ]]; then
    max_bytes=16384
  fi

  rel="history/pg_restore_${IRT_RUN_ID}.error.log"
  dest="${IRT_ENV_EVIDENCE_DIR}/${rel}"
  latest="${IRT_ENV_EVIDENCE_DIR}/last-pg-restore-error.log"
  tmp="${dest}.tmp.$$.$RANDOM"

  if ! irt_sanitize_pg_restore_diag <"$src" >"$tmp" 2>/dev/null; then
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi

  size="$(wc -c <"$tmp" 2>/dev/null | tr -d '[:space:]' || echo 0)"
  if [[ ! "$size" =~ ^[0-9]+$ ]]; then
    size=0
  fi

  trailer="$(printf '\n[truncated to %s bytes]\n' "$max_bytes")"
  trailer_bytes="$(printf '%s' "$trailer" | wc -c | tr -d '[:space:]')"
  if [[ ! "$trailer_bytes" =~ ^[0-9]+$ ]]; then
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi

  if (( size > max_bytes )); then
    truncated="${tmp}.trunc"
    if (( trailer_bytes >= max_bytes )); then
      # Extreme/tiny cap: publish a hard-clipped trailer only.
      if ! printf '%s' "$trailer" | head -c "$max_bytes" >"$truncated" 2>/dev/null; then
        rm -f -- "$tmp" "$truncated" 2>/dev/null || true
        return 1
      fi
    else
      content_budget=$((max_bytes - trailer_bytes))
      if ! head -c "$content_budget" "$tmp" >"$truncated" 2>/dev/null; then
        rm -f -- "$tmp" "$truncated" 2>/dev/null || true
        return 1
      fi
      if ! printf '%s' "$trailer" >>"$truncated" 2>/dev/null; then
        rm -f -- "$tmp" "$truncated" 2>/dev/null || true
        return 1
      fi
    fi
    mv -f -- "$truncated" "$tmp" 2>/dev/null || {
      rm -f -- "$tmp" "$truncated" 2>/dev/null || true
      return 1
    }
  fi

  # Belt-and-suspenders: never publish more than max_bytes.
  size="$(wc -c <"$tmp" 2>/dev/null | tr -d '[:space:]' || echo 0)"
  if [[ "$size" =~ ^[0-9]+$ ]] && (( size > max_bytes )); then
    truncated="${tmp}.hardcap"
    if ! head -c "$max_bytes" "$tmp" >"$truncated" 2>/dev/null; then
      rm -f -- "$tmp" "$truncated" 2>/dev/null || true
      return 1
    fi
    mv -f -- "$truncated" "$tmp" 2>/dev/null || {
      rm -f -- "$tmp" "$truncated" 2>/dev/null || true
      return 1
    }
  fi

  if ! chmod 600 "$tmp" 2>/dev/null; then
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! mv -f -- "$tmp" "$dest" 2>/dev/null; then
    rm -f -- "$tmp" 2>/dev/null || true
    return 1
  fi
  chmod 600 "$dest" 2>/dev/null || true

  # Atomic active pointer in the same directory (mv replaces a symlink entry; does not follow it).
  latest_tmp="${IRT_ENV_EVIDENCE_DIR}/last-pg-restore-error.log.tmp.$$.$RANDOM"
  if [[ -L "$latest" ]]; then
    rm -f -- "$latest" 2>/dev/null || true
  fi
  if cp -f -- "$dest" "$latest_tmp" 2>/dev/null \
    && chmod 600 "$latest_tmp" 2>/dev/null \
    && mv -f -- "$latest_tmp" "$latest" 2>/dev/null; then
    chmod 600 "$latest" 2>/dev/null || true
  else
    rm -f -- "$latest_tmp" 2>/dev/null || true
    # Do not leave a partial/wrong active file; history path still links the failure.
  fi

  IRT_PG_RESTORE_ERROR_LOG="$rel"
  return 0
}

irt_clear_active_pg_restore_diagnostic() {
  # Success must not leave an active false error log. History copies for prior
  # RUN_IDs are retained until prune.
  IRT_PG_RESTORE_ERROR_LOG=""
  if [[ -n "${IRT_ENV_EVIDENCE_DIR:-}" ]]; then
    rm -f -- "${IRT_ENV_EVIDENCE_DIR}/last-pg-restore-error.log" 2>/dev/null || true
  fi
}

# Map interruptible command status: parent interrupt → exit 50 INTERRUPTED;
# child signal death (>128) → fail-closed rc=50 with phase ERROR_CODE;
# normal nonzero → phase_rc + phase_err.
irt_require_cmd_ok() {
  local st="$1"
  local phase_rc="$2"
  local phase_err="$3"
  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
    record_failure 50 "INTERRUPTED"
    exit 50
  fi
  if (( st > 128 )); then
    fail 50 "$phase_err"
  fi
  if [[ "$st" -ne 0 ]]; then
    fail "$phase_rc" "$phase_err"
  fi
}

irt_apply_parent_signal_lock() {
  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
    IRT_WORK_OK=0
    IRT_STATUS="failed"
    IRT_EXIT_CODE=50
    IRT_ERROR_CODE="INTERRUPTED"
  fi
}

finalize_once() {
  local incoming=$?
  if [[ "$IRT_FINALIZED" -eq 1 ]]; then
    # Idempotent re-entry: never flip a recorded interrupt/failure to success.
    irt_apply_parent_signal_lock
    if (( IRT_EXIT_CODE > 128 )); then
      IRT_EXIT_CODE=50
    fi
    if [[ "$IRT_EXIT_CODE" -eq 0 && "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
      IRT_EXIT_CODE=50
      IRT_STATUS="failed"
      IRT_ERROR_CODE="INTERRUPTED"
    fi
    exit "${IRT_EXIT_CODE:-$incoming}"
  fi
  IRT_FINALIZED=1
  disarm_traps

  # Parent INT/TERM always wins over IRT_WORK_OK / incoming 0.
  irt_apply_parent_signal_lock

  # Map raw >128 exits to operational rc=50. INTERRUPTED only if parent trap fired.
  if (( incoming > 128 )); then
    IRT_EXIT_CODE=50
    IRT_STATUS="failed"
    if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
      IRT_ERROR_CODE="${IRT_ERROR_CODE:-INTERRUPTED}"
    else
      IRT_ERROR_CODE="${IRT_ERROR_CODE:-UNEXPECTED_ERROR}"
    fi
  fi

  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -ne 1 && "$IRT_EXIT_CODE" -eq 0 && "$incoming" -ne 0 ]]; then
    IRT_EXIT_CODE="$incoming"
  fi
  if [[ "$IRT_EXIT_CODE" -ne 0 && -z "$IRT_ERROR_CODE" ]]; then
    IRT_ERROR_CODE="UNEXPECTED_ERROR"
    IRT_STATUS="failed"
  fi

  # Always attempt cleanup before evidence.
  cleanup_temp_resources || true
  verify_cleanup_proof || true

  irt_apply_parent_signal_lock

  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -ne 1 \
     && "$IRT_WORK_OK" -eq 1 && "$IRT_CLEANUP_OK" -eq 1 \
     && "$IRT_TEMP_ABSENT" -eq 1 && "$IRT_SNAPSHOT_ABSENT" -eq 1 ]]; then
    IRT_STATUS="success"
    IRT_ERROR_CODE=""
    IRT_EXIT_CODE=0
  else
    IRT_STATUS="failed"
    if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
      IRT_EXIT_CODE=50
      IRT_ERROR_CODE="INTERRUPTED"
    elif [[ "$IRT_EXIT_CODE" -eq 0 ]]; then
      # Restore path looked OK but cleanup/evidence proof incomplete.
      IRT_EXIT_CODE=50
      IRT_ERROR_CODE="${IRT_ERROR_CODE:-CLEANUP_INCOMPLETE}"
    elif [[ "$IRT_WORK_OK" -eq 1 && ( "$IRT_CLEANUP_OK" -ne 1 || "$IRT_TEMP_ABSENT" -ne 1 || "$IRT_SNAPSHOT_ABSENT" -ne 1 ) ]]; then
      # Preserve original restore error if any; still force nonzero for cleanup.
      if [[ "$IRT_EXIT_CODE" -eq 0 ]]; then
        IRT_EXIT_CODE=50
      fi
      if [[ -z "$IRT_ERROR_CODE" || "$IRT_ERROR_CODE" == "" ]]; then
        IRT_ERROR_CODE="CLEANUP_INCOMPLETE"
      fi
    fi
  fi

  if [[ "$IRT_DRY_RUN" -eq 0 && "$IRT_SKIP_EVIDENCE" -eq 0 && "$IRT_LOCK_HELD" -eq 1 ]]; then
    if ! write_attempt_evidence; then
      IRT_STATUS="failed"
      IRT_EXIT_CODE=50
      IRT_ERROR_CODE="EVIDENCE_WRITE_FAILED"
      IRT_WORK_OK=0
      irt_info "ISOLATED_RESTORE_TEST FAIL env=${IRT_ENV:-unknown} code=EVIDENCE_WRITE_FAILED phase=evidence"
    fi
  fi

  irt_apply_parent_signal_lock

  # Success requires work + cleanup proofs + evidence written as success + no parent signal.
  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -ne 1 \
     && "$IRT_WORK_OK" -eq 1 && "$IRT_CLEANUP_OK" -eq 1 && "$IRT_TEMP_ABSENT" -eq 1 \
     && "$IRT_SNAPSHOT_ABSENT" -eq 1 && "$IRT_STATUS" == "success" && "$IRT_EXIT_CODE" -eq 0 ]]; then
    :
  elif [[ "$IRT_EXIT_CODE" -eq 0 ]]; then
    IRT_EXIT_CODE=50
    IRT_STATUS="failed"
    if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
      IRT_ERROR_CODE="INTERRUPTED"
    else
      IRT_ERROR_CODE="${IRT_ERROR_CODE:-CLEANUP_INCOMPLETE}"
    fi
  fi

  # Never exit with a raw signal status; INTERRUPTED only for parent trap.
  if (( IRT_EXIT_CODE > 128 )); then
    IRT_EXIT_CODE=50
    IRT_STATUS="failed"
    if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
      IRT_ERROR_CODE="${IRT_ERROR_CODE:-INTERRUPTED}"
    else
      IRT_ERROR_CODE="${IRT_ERROR_CODE:-UNEXPECTED_ERROR}"
    fi
  fi

  irt_apply_parent_signal_lock

  if [[ "$IRT_STATUS" == "success" ]]; then
    irt_info "ISOLATED_RESTORE_TEST SUCCESS env=${IRT_ENV} dump=${IRT_DUMP_BASENAME} tables=${IRT_USER_TABLE_COUNT} schemas=${IRT_USER_SCHEMA_COUNT} durationSec=$(irt_duration_sec)"
  elif [[ "$IRT_DRY_RUN" -eq 0 && "$IRT_SKIP_EVIDENCE" -eq 0 ]]; then
    irt_info "ISOLATED_RESTORE_TEST FAIL env=${IRT_ENV:-unknown} code=${IRT_ERROR_CODE:-unknown} phase=${IRT_PHASE:-unknown} cleanup=${IRT_CLEANUP_OK} absent=${IRT_TEMP_ABSENT} durationSec=$(irt_duration_sec)"
  fi

  exit "$IRT_EXIT_CODE"
}

install_lifecycle_traps() {
  trap finalize_once EXIT
  trap on_err ERR
  trap on_signal INT TERM
}

# --- lock / dump / snapshot -------------------------------------------------

acquire_lock() {
  irt_ensure_evidence_dirs
  local lock="${IRT_EVIDENCE_ROOT}/${IRT_LOCK_NAME}"
  exec 9>"$lock"
  if ! flock -n 9; then
    irt_info "ISOLATED_RESTORE_TEST SKIP concurrent run"
    IRT_EXIT_CODE=60
    IRT_ERROR_CODE="LOCK_HELD"
    IRT_STATUS="failed"
    # No resources created; skip evidence rewrite in finalizer.
    IRT_SKIP_EVIDENCE=1
    exit 60
  fi
  IRT_LOCK_HELD=1
}

prepare_run_identity() {
  IRT_RUN_ID="$(irt_random_hex 8)"
  [[ "$IRT_RUN_ID" =~ $IRT_RUN_ID_RE ]] || fail 70 "RUN_ID_INVALID"
  IRT_RUN_DIR="${IRT_RUNTIME_DIR}/${IRT_RUN_ID}"
  mkdir -p "$IRT_RUN_DIR"
  chmod 700 "$IRT_RUN_DIR"
  IRT_CIDFILE="${IRT_RUN_DIR}/container.cid"
  IRT_CURRENT_MARKER="${IRT_RUNTIME_DIR}/current.env"
  IRT_SNAPSHOT_PATH="${IRT_RUN_DIR}/dump.snapshot"
  # Protected marker for ExecStopPost (mode 0600).
  umask 0077
  cat >"$IRT_CURRENT_MARKER" <<EOF
RUN_ID=${IRT_RUN_ID}
CIDFILE=${IRT_CIDFILE}
ENVIRONMENT=${IRT_ENV}
STARTED_AT_EPOCH=${IRT_STARTED_EPOCH}
EOF
  chmod 600 "$IRT_CURRENT_MARKER"
}

select_dump() {
  IRT_PHASE="select_dump"
  local path
  if [[ -n "$IRT_DUMP_ARG" ]]; then
    path="$(irt_validate_dump_path "$IRT_DUMP_ARG" || true)"
    [[ -n "$path" ]] || fail 10 "DUMP_INVALID_PATH"
  else
    path="$(irt_newest_dump "$IRT_DUMP_DIR" || true)"
    [[ -n "$path" ]] || fail 10 "DUMP_MISSING"
  fi
  IRT_DUMP_PATH="$path"
  IRT_DUMP_BASENAME="$(basename -- "$IRT_DUMP_PATH")"
  read -r IRT_DUMP_DEV IRT_DUMP_INODE IRT_DUMP_SIZE IRT_DUMP_MTIME_EPOCH <<<"$(irt_file_identity "$IRT_DUMP_PATH")"
  IRT_DUMP_AGE_HOURS="$(irt_dump_age_hours "$IRT_DUMP_PATH")"
  IRT_DUMP_MTIME_UTC="$(date -u -d "@${IRT_DUMP_MTIME_EPOCH}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || date -u -r "${IRT_DUMP_MTIME_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)"
  if (( IRT_DUMP_AGE_HOURS > IRT_DUMP_MAX_AGE_HOURS )); then
    fail 10 "DUMP_STALE"
  fi
  if [[ ! -r "$IRT_DUMP_PATH" ]]; then
    fail 10 "DUMP_UNREADABLE"
  fi
  IRT_DUMP_SHA="$(irt_sha256_file "$IRT_DUMP_PATH")"
}

create_dump_snapshot() {
  IRT_PHASE="snapshot"
  local tmp_snap id_after sha_after
  tmp_snap="${IRT_SNAPSHOT_PATH}.partial"

  # Copy without mutating the original (reflink when available).
  if ! cp --reflink=auto -- "$IRT_DUMP_PATH" "$tmp_snap" 2>/dev/null; then
    cp -- "$IRT_DUMP_PATH" "$tmp_snap" || fail 10 "SNAPSHOT_COPY_FAILED"
  fi
  chmod 400 "$tmp_snap"

  # Re-validate source identity after copy (TOCTOU detection).
  local dev2 inode2 size2 mtime2 sha_after
  read -r dev2 inode2 size2 mtime2 <<<"$(irt_file_identity "$IRT_DUMP_PATH")"
  sha_after="$(irt_sha256_file "$IRT_DUMP_PATH")"
  if [[ "$dev2" != "$IRT_DUMP_DEV" || "$inode2" != "$IRT_DUMP_INODE" \
     || "$size2" != "$IRT_DUMP_SIZE" || "$mtime2" != "$IRT_DUMP_MTIME_EPOCH" \
     || "$sha_after" != "$IRT_DUMP_SHA" ]]; then
    rm -f -- "$tmp_snap"
    fail 10 "DUMP_TOCTOU_DETECTED"
  fi

  IRT_SNAPSHOT_SHA="$(irt_sha256_file "$tmp_snap")"
  if [[ "$IRT_SNAPSHOT_SHA" != "$IRT_DUMP_SHA" ]]; then
    rm -f -- "$tmp_snap"
    fail 10 "SNAPSHOT_HASH_MISMATCH"
  fi

  mv -f -- "$tmp_snap" "$IRT_SNAPSHOT_PATH"
  chmod 400 "$IRT_SNAPSHOT_PATH"
  IRT_SNAPSHOT_ABSENT=0
}

verify_snapshot_before_mount() {
  IRT_PHASE="snapshot_verify"
  local sha size
  [[ -f "$IRT_SNAPSHOT_PATH" && ! -L "$IRT_SNAPSHOT_PATH" ]] || fail 10 "SNAPSHOT_MISSING"
  size="$(stat -c '%s' "$IRT_SNAPSHOT_PATH")"
  sha="$(irt_sha256_file "$IRT_SNAPSHOT_PATH")"
  [[ "$size" == "$IRT_DUMP_SIZE" && "$sha" == "$IRT_DUMP_SHA" ]] || fail 10 "SNAPSHOT_TAMPERED"
  IRT_SNAPSHOT_SHA="$sha"
}

verify_snapshot_after_restore() {
  local sha
  sha="$(irt_sha256_file "$IRT_SNAPSHOT_PATH")"
  [[ "$sha" == "$IRT_DUMP_SHA" ]] || fail 10 "SNAPSHOT_CHANGED_DURING_RESTORE"
}

# --- docker / restore -------------------------------------------------------

assert_image_present() {
  IRT_PHASE="image_check"
  if ! docker image inspect "$IRT_PG_IMAGE" >/dev/null 2>&1; then
    fail 20 "PG_IMAGE_MISSING"
  fi
}

capture_forbidden_pre() {
  if [[ "$IRT_SKIP_FORBIDDEN_CHECK" == "1" ]]; then
    return 0
  fi
  IRT_FORBIDDEN_PRE="$(irt_forbidden_snapshot || true)"
}

assert_forbidden_unchanged() {
  if [[ "$IRT_SKIP_FORBIDDEN_CHECK" == "1" ]]; then
    return 0
  fi
  local post
  post="$(irt_forbidden_snapshot || true)"
  if [[ "$post" != "$IRT_FORBIDDEN_PRE" ]]; then
    fail 80 "FORBIDDEN_CONTAINER_CHANGED"
  fi
}

start_temp_postgres() {
  IRT_PHASE="start_temp_pg"
  IRT_TEMP_CONTAINER="bot-tv-rt-staging-${IRT_RUN_ID}"
  irt_is_safe_temp_name "$IRT_TEMP_CONTAINER" || fail 70 "TEMP_NAME_INVALID"
  IRT_TEMP_PASSWORD="$(irt_random_hex 24)"

  # Ensure cidfile does not exist (docker refuses if present).
  rm -f -- "$IRT_CIDFILE"

  # Interruptible: SIGTERM during docker run must reach on_signal/finalizer.
  local run_rc=0
  trap - ERR
  set +e
  irt_interruptible_run docker run -d \
    --cidfile "$IRT_CIDFILE" \
    --name "$IRT_TEMP_CONTAINER" \
    --label "${IRT_LABEL_COMPONENT}=${IRT_COMPONENT_VALUE}" \
    --label "${IRT_LABEL_ENV}=${IRT_ENV}" \
    --label "${IRT_LABEL_RUN}=${IRT_RUN_ID}" \
    --network none \
    --pull=never \
    --memory="$IRT_DOCKER_MEMORY" \
    --cpus="$IRT_DOCKER_CPUS" \
    --pids-limit="$IRT_DOCKER_PIDS_LIMIT" \
    -e "POSTGRES_PASSWORD=${IRT_TEMP_PASSWORD}" \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_DB=postgres \
    -v "${IRT_SNAPSHOT_PATH}:/restore-source.dump:ro" \
    "$IRT_PG_IMAGE" >/dev/null
  run_rc=$?
  set -e
  trap on_err ERR
  if [[ "$run_rc" -ne 0 ]]; then
    IRT_TEMP_CONTAINER=""
  fi
  irt_require_cmd_ok "$run_rc" 20 "TEMP_PG_START_FAILED"

  IRT_TEMP_CID="$(tr -d '[:space:]' <"$IRT_CIDFILE" | tr '[:upper:]' '[:lower:]')"
  irt_is_safe_cid "$IRT_TEMP_CID" || fail 20 "TEMP_CID_INVALID"
  chmod 600 "$IRT_CIDFILE" 2>/dev/null || true

  local waited=0
  local ready_rc=0
  while (( waited < IRT_PG_READY_TIMEOUT_SEC )); do
    set +e
    irt_interruptible_run docker exec -e "PGPASSWORD=${IRT_TEMP_PASSWORD}" "$IRT_TEMP_CID" \
      pg_isready -U postgres -d postgres >/dev/null 2>&1
    ready_rc=$?
    set -e
    if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
      exit 50
    fi
    if (( ready_rc > 128 )); then
      fail 50 "TEMP_PG_START_FAILED"
    fi
    if [[ "$ready_rc" -eq 0 ]]; then
      return 0
    fi
    set +e
    irt_interruptible_run sleep 2
    set -e
    if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
      exit 50
    fi
    waited=$((waited + 2))
  done
  fail 20 "TEMP_PG_NOT_READY"
}

run_restore() {
  IRT_PHASE="pg_restore"
  local create_rc=0
  trap - ERR
  set +e
  irt_interruptible_run docker exec -e "PGPASSWORD=${IRT_TEMP_PASSWORD}" "$IRT_TEMP_CID" \
    psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE restore_test;" >/dev/null 2>&1
  create_rc=$?
  set -e
  trap on_err ERR
  irt_require_cmd_ok "$create_rc" 30 "CREATE_DB_FAILED"

  local rc=0
  local restore_log="${IRT_RUN_DIR}/pg_restore.log"
  : >"$restore_log"
  chmod 600 "$restore_log" 2>/dev/null || true
  # Explicit status capture: disarm ERR so an expected nonzero is not "unexpected".
  # --no-owner --no-acl: isolated container has no source roles (e.g. tvoe_vremya);
  # restore-test verifies dump portability/integrity, not original ownership/ACLs.
# Source roles (e.g. bot_tv_stage) are not required inside the temp container.
  trap - ERR
  set +e
  irt_interruptible_capture_merged "$restore_log" docker exec -e "PGPASSWORD=${IRT_TEMP_PASSWORD}" "$IRT_TEMP_CID" \
    pg_restore -U postgres -d restore_test --exit-on-error --no-owner --no-acl /restore-source.dump
  rc=$?
  set -e
  trap on_err ERR
  if [[ "${IRT_SIGNAL_RECEIVED:-0}" -ne 1 && "$rc" -ne 0 ]]; then
    # Diagnostic is best-effort; never masks PG_RESTORE_FAILED below.
    irt_publish_pg_restore_diagnostic "$restore_log" || true
  fi
  irt_require_cmd_ok "$rc" 30 "PG_RESTORE_FAILED"
}

run_integrity_checks() {
  IRT_PHASE="integrity"
  local schemas tables
  local schemas_file tables_file
  schemas_file="${IRT_RUN_DIR}/schemas.count"
  tables_file="${IRT_RUN_DIR}/tables.count"
  local sc_rc=0 tc_rc=0 cat_rc=0

  trap - ERR
  set +e
  irt_interruptible_capture "$schemas_file" docker exec -e "PGPASSWORD=${IRT_TEMP_PASSWORD}" "$IRT_TEMP_CID" \
    psql -U postgres -d restore_test -At -c \
    "SELECT count(*) FROM information_schema.schemata WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast');"
  sc_rc=$?
  set -e
  trap on_err ERR
  irt_require_cmd_ok "$sc_rc" 40 "INTEGRITY_QUERY_FAILED"
  schemas="$(tr -d '\r' <"$schemas_file" 2>/dev/null || true)"

  trap - ERR
  set +e
  irt_interruptible_capture "$tables_file" docker exec -e "PGPASSWORD=${IRT_TEMP_PASSWORD}" "$IRT_TEMP_CID" \
    psql -U postgres -d restore_test -At -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_type='BASE TABLE';"
  tc_rc=$?
  set -e
  trap on_err ERR
  irt_require_cmd_ok "$tc_rc" 40 "INTEGRITY_QUERY_FAILED"
  tables="$(tr -d '\r' <"$tables_file" 2>/dev/null || true)"
  rm -f -- "$schemas_file" "$tables_file" 2>/dev/null || true

  if [[ ! "$schemas" =~ ^[0-9]+$ || ! "$tables" =~ ^[0-9]+$ ]]; then
    fail 40 "INTEGRITY_QUERY_FAILED"
  fi
  IRT_USER_SCHEMA_COUNT="$schemas"
  IRT_USER_TABLE_COUNT="$tables"
  if (( IRT_USER_TABLE_COUNT < 1 )); then
    fail 40 "INTEGRITY_NO_USER_TABLES"
  fi

  trap - ERR
  set +e
  irt_interruptible_run docker exec -e "PGPASSWORD=${IRT_TEMP_PASSWORD}" "$IRT_TEMP_CID" \
    psql -U postgres -d restore_test -v ON_ERROR_STOP=1 -c \
    "SELECT 1 FROM pg_catalog.pg_class LIMIT 1; SELECT count(*) FROM pg_catalog.pg_namespace;" \
    >/dev/null 2>&1
  cat_rc=$?
  set -e
  trap on_err ERR
  irt_require_cmd_ok "$cat_rc" 40 "INTEGRITY_CATALOG_FAILED"
  IRT_INTEGRITY_OK=1
}

# --- migration proof (not used in bot-TV) -----------------------------------
# bot-TV uses Alembic operationally; isolated restore-test proves dump restore
# integrity only (schema/user tables). No Prisma migration-proof contour here.

run_migration_proof() {
  if [[ "${IRT_MIGRATION_PROOF:-0}" -eq 1 ]]; then
    fail 70 "MIGRATION_PROOF_UNSUPPORTED"
  fi
  return 0
}

# --- cleanup ----------------------------------------------------------------

# Remove known per-run temp files inside a validated runtime/<run-id> directory.
# Never uses rm -rf on the run dir itself; callers ensure owned canonical path.
# keep_cid=1 retains container.cid (emergency keeps it until evidence is fixed).
irt_purge_run_dir_files() {
  local run_dir="$1"
  local keep_cid="${2:-0}"
  [[ -n "$run_dir" && -d "$run_dir" ]] || return 0
  rm -f -- \
    "${run_dir}/dump.snapshot" \
    "${run_dir}/dump.snapshot.partial" \
    "${run_dir}/schemas.count" \
    "${run_dir}/tables.count" \
    "${run_dir}/pg_restore.log" \
    2>/dev/null || true
  if [[ -d "${run_dir}/proof-source" ]]; then
    rm -rf -- "${run_dir}/proof-source"
  fi
  if [[ "$keep_cid" != "1" ]]; then
    rm -f -- "${run_dir}/container.cid" 2>/dev/null || true
  fi
}

remove_owned_container_by_ref() {
  local ref="$1"
  local expect_env="$2"
  local expect_run="${3-}"
  local rm_rc=0

  if ! irt_is_safe_cid "$ref" && ! irt_is_safe_temp_name "$ref"; then
    return 2
  fi

  if ! irt_container_exists "$ref"; then
    return 0
  fi

  if ! irt_validate_owned_container "$ref" "$expect_env" "$expect_run"; then
    return 3
  fi

  trap - ERR
  set +e
  docker rm -f -- "$ref" >/dev/null 2>&1
  rm_rc=$?
  set -e
  if [[ "$IRT_FINALIZED" -eq 0 ]]; then
    trap on_err ERR
  fi
  IRT_DOCKER_RM_RC="$rm_rc"

  if irt_container_exists "$ref"; then
    return 1
  fi
  # Absent after attempt → success even if docker rm returned nonzero (already gone).
  return 0
}

cleanup_temp_resources() {
  IRT_PHASE="cleanup"
  local container_ok=1 snapshot_ok=1 marker_ok=1 rundir_ok=1

  # Idempotent: safe when container never started.
  if [[ -n "${IRT_TEMP_CID:-}" ]]; then
    if ! remove_owned_container_by_ref "$IRT_TEMP_CID" "$IRT_ENV" "$IRT_RUN_ID"; then
      # Try by name as secondary if cid stale.
      if [[ -n "${IRT_TEMP_CONTAINER:-}" ]]; then
        if ! remove_owned_container_by_ref "$IRT_TEMP_CONTAINER" "$IRT_ENV" "$IRT_RUN_ID"; then
          container_ok=0
        fi
      else
        container_ok=0
      fi
    fi
  elif [[ -n "${IRT_TEMP_CONTAINER:-}" ]]; then
    if ! remove_owned_container_by_ref "$IRT_TEMP_CONTAINER" "$IRT_ENV" "$IRT_RUN_ID"; then
      container_ok=0
    fi
  fi

  if [[ -n "${IRT_SNAPSHOT_PATH:-}" && -e "${IRT_SNAPSHOT_PATH}" ]]; then
    rm -f -- "$IRT_SNAPSHOT_PATH" || snapshot_ok=0
  fi
  if [[ -n "${IRT_RUN_DIR:-}" && -d "${IRT_RUN_DIR}" ]]; then
    irt_purge_run_dir_files "$IRT_RUN_DIR"
    if ! rmdir "$IRT_RUN_DIR" 2>/dev/null; then
      irt_purge_run_dir_files "$IRT_RUN_DIR"
      rmdir "$IRT_RUN_DIR" 2>/dev/null || rundir_ok=0
    fi
  fi
  if [[ -n "${IRT_RUNTIME_DIR:-}" ]]; then
    rm -f -- "${IRT_RUNTIME_DIR}/.pause-after-work-ok" 2>/dev/null || true
  fi
  if [[ -n "${IRT_CURRENT_MARKER:-}" && -f "${IRT_CURRENT_MARKER}" ]]; then
    # Only remove marker if it points to this run.
    if grep -q "RUN_ID=${IRT_RUN_ID}" "$IRT_CURRENT_MARKER" 2>/dev/null; then
      rm -f -- "$IRT_CURRENT_MARKER" || marker_ok=0
    fi
  fi

  IRT_TEMP_PASSWORD=""

  # Offline runner is a pre-provisioned shared artifact; proof cleanup never removes it.

  if [[ "$container_ok" -eq 1 && "$snapshot_ok" -eq 1 && "$marker_ok" -eq 1 && "$rundir_ok" -eq 1 ]]; then
    IRT_CLEANUP_OK=1
  else
    IRT_CLEANUP_OK=0
  fi
}

verify_cleanup_proof() {
  IRT_TEMP_ABSENT=0
  IRT_SNAPSHOT_ABSENT=0

  local docker_ok=1 leftover=0 f
  if ! command -v docker >/dev/null 2>&1; then
    docker_ok=0
  elif ! docker info >/dev/null 2>&1; then
    docker_ok=0
  fi

  # Container absence
  if [[ -z "${IRT_TEMP_CID:-}" && -z "${IRT_TEMP_CONTAINER:-}" ]]; then
    IRT_TEMP_ABSENT=1
  elif [[ "$docker_ok" -eq 0 ]]; then
    # Cannot prove absence.
    IRT_TEMP_ABSENT=0
    IRT_CLEANUP_OK=0
  else
    local still=0
    if [[ -n "${IRT_TEMP_CID:-}" ]] && irt_container_exists "$IRT_TEMP_CID"; then
      still=1
    fi
    if [[ -n "${IRT_TEMP_CONTAINER:-}" ]] && irt_container_exists "$IRT_TEMP_CONTAINER"; then
      still=1
    fi
    if [[ "$still" -eq 0 ]]; then
      IRT_TEMP_ABSENT=1
    else
      IRT_TEMP_ABSENT=0
      IRT_CLEANUP_OK=0
    fi
  fi

  # Snapshot absence
  if [[ -z "${IRT_SNAPSHOT_PATH:-}" || ! -e "${IRT_SNAPSHOT_PATH}" ]]; then
    IRT_SNAPSHOT_ABSENT=1
  else
    IRT_SNAPSHOT_ABSENT=0
    IRT_CLEANUP_OK=0
  fi

  # Known per-run leftovers must not remain (raw pg_restore.log included).
  if [[ -n "${IRT_RUN_DIR:-}" ]]; then
    leftover=0
    for f in pg_restore.log schemas.count tables.count dump.snapshot.partial; do
      if [[ -e "${IRT_RUN_DIR}/${f}" ]]; then
        leftover=1
        break
      fi
    done
    # After cidfile removal, an empty leftover run-dir is still incomplete cleanup.
    if [[ -d "${IRT_RUN_DIR}" && ! -e "${IRT_RUN_DIR}/container.cid" ]]; then
      leftover=1
    fi
    if [[ "$leftover" -eq 1 ]]; then
      IRT_TEMP_ABSENT=0
      IRT_CLEANUP_OK=0
    fi
  fi

  # CLEANUP_OK only if proofs hold.
  if [[ "$IRT_CLEANUP_OK" -eq 1 && "$IRT_TEMP_ABSENT" -eq 1 && "$IRT_SNAPSHOT_ABSENT" -eq 1 ]]; then
    IRT_CLEANUP_OK=1
  else
    IRT_CLEANUP_OK=0
  fi
}

# --- evidence ---------------------------------------------------------------

capture_finished_epoch() {
  # Must run in the parent shell (not inside process-substitution subshell).
  if [[ ! "${IRT_FINISHED_EPOCH:-0}" =~ ^[1-9][0-9]*$ ]]; then
    IRT_FINISHED_EPOCH="$(date +%s)"
  fi
  if [[ ! "${IRT_STARTED_EPOCH:-0}" =~ ^[1-9][0-9]*$ ]]; then
    IRT_STARTED_EPOCH="$IRT_FINISHED_EPOCH"
  fi
  if (( IRT_FINISHED_EPOCH < IRT_STARTED_EPOCH )); then
    IRT_FINISHED_EPOCH="$IRT_STARTED_EPOCH"
  fi
}

irt_duration_sec() {
  capture_finished_epoch
  echo $((IRT_FINISHED_EPOCH - IRT_STARTED_EPOCH))
}

evidence_lines() {
  # Reads IRT_FINISHED_EPOCH / IRT_STARTED_EPOCH; does not assign them (N-02).
  local started finished duration temp_started restore_ok sql_ok
  started="$(date -u -d "@${IRT_STARTED_EPOCH}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  finished="$(date -u -d "@${IRT_FINISHED_EPOCH}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  duration=$((IRT_FINISHED_EPOCH - IRT_STARTED_EPOCH))
  if (( duration < 0 )); then
    duration=0
  fi
  temp_started=0
  [[ -n "${IRT_TEMP_CID:-}${IRT_TEMP_CONTAINER:-}" ]] && temp_started=1
  restore_ok=0
  sql_ok=0
  if [[ "$IRT_INTEGRITY_OK" -eq 1 ]]; then
    restore_ok=1
    sql_ok=1
  fi
  cat <<EOF
SCHEMA_VERSION=1
ENVIRONMENT=$(irt_escape_manifest_value "$IRT_ENV")
STATUS=$(irt_escape_manifest_value "$IRT_STATUS")
ERROR_CODE=$(irt_escape_manifest_value "${IRT_ERROR_CODE:-}")
STARTED_AT_UTC=$(irt_escape_manifest_value "$started")
FINISHED_AT_UTC=$(irt_escape_manifest_value "$finished")
DURATION_SEC=${duration}
RUN_ID=$(irt_escape_manifest_value "${IRT_RUN_ID:-}")
DUMP_BASENAME=$(irt_escape_manifest_value "$IRT_DUMP_BASENAME")
DUMP_MTIME_UTC=$(irt_escape_manifest_value "$IRT_DUMP_MTIME_UTC")
DUMP_SIZE_BYTES=${IRT_DUMP_SIZE}
DUMP_SHA256=$(irt_escape_manifest_value "${IRT_SNAPSHOT_SHA:-$IRT_DUMP_SHA}")
DUMP_AGE_HOURS=${IRT_DUMP_AGE_HOURS}
PG_IMAGE=$(irt_escape_manifest_value "$IRT_PG_IMAGE")
TEMP_CONTAINER=$(irt_escape_manifest_value "${IRT_TEMP_CONTAINER:-}")
TEMP_CID=$(irt_escape_manifest_value "${IRT_TEMP_CID:-}")
TEMP_PG_STARTED=${temp_started}
RESTORE_OK=${restore_ok}
SQL_CONNECT_OK=${sql_ok}
USER_SCHEMA_COUNT=${IRT_USER_SCHEMA_COUNT}
USER_TABLE_COUNT=${IRT_USER_TABLE_COUNT}
INTEGRITY_OK=${IRT_INTEGRITY_OK}
MIGRATION_PROOF=${IRT_MIGRATION_PROOF}
TARGET_REVISION=$(irt_escape_manifest_value "${IRT_TARGET_REV_ARG:-}")
MIGRATION_20260806=$(irt_escape_manifest_value "$IRT_PROOF_20260806")
MIGRATION_20260807=$(irt_escape_manifest_value "$IRT_PROOF_20260807")
MIGRATION_20260819=$(irt_escape_manifest_value "$IRT_PROOF_20260819")
FINAL_MIGRATION_STATUS=$(irt_escape_manifest_value "$IRT_PROOF_FINAL_STATUS")
CLEANUP_OK=${IRT_CLEANUP_OK}
TEMP_RESOURCES_ABSENT=${IRT_TEMP_ABSENT}
SNAPSHOT_ABSENT=${IRT_SNAPSHOT_ABSENT}
PG_RESTORE_ERROR_LOG=$(irt_escape_manifest_value "${IRT_PG_RESTORE_ERROR_LOG:-}")
EOF
}

write_attempt_evidence() {
  # Fail-closed: any inability to publish mandatory last-attempt (and history)
  # is a hard error. Must return nonzero even when invoked under `if ! ...`
  # (where set -e is suppressed for the function body).
  if ! irt_ensure_evidence_dirs; then
    echo "ISOLATED_RESTORE_TEST evidence dirs unavailable" >&2
    return 1
  fi
  capture_finished_epoch
  local ts hist attempt uniq
  # Collision-safe: UTC timestamp + PID + run-id; create with no-clobber.
  ts="$(date -u +%Y%m%dT%H%M%S%N 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)"
  uniq="${ts}_$$_${IRT_RUN_ID:-norun}"
  attempt="${IRT_ENV_EVIDENCE_DIR}/last-attempt.env"
  hist="${IRT_HISTORY_DIR}/${uniq}_${IRT_STATUS}.env"
  if [[ "$IRT_STATUS" == "success" ]]; then
    # Drop active error pointer before publishing success manifests.
    irt_clear_active_pg_restore_diagnostic
  fi
  local lines
  mapfile -t lines < <(evidence_lines) || return 1
  if ! irt_write_evidence_file "$attempt" "${lines[@]}"; then
    echo "ISOLATED_RESTORE_TEST evidence write failed (last-attempt)" >&2
    return 1
  fi
  if [[ -e "$hist" ]]; then
    hist="${IRT_HISTORY_DIR}/${uniq}_${RANDOM}_${IRT_STATUS}.env"
  fi
  if ! irt_write_evidence_file "$hist" "${lines[@]}"; then
    echo "ISOLATED_RESTORE_TEST evidence write failed (history)" >&2
    return 1
  fi
  irt_prune_history || true
  if [[ "$IRT_STATUS" == "success" ]]; then
    if ! irt_write_evidence_file "${IRT_ENV_EVIDENCE_DIR}/last-success.env" "${lines[@]}"; then
      echo "ISOLATED_RESTORE_TEST evidence write failed (last-success)" >&2
      return 1
    fi
  fi
  return 0
}

irt_attempt_finalized_for_run() {
  # True when last-attempt already records a complete result for this RUN_ID.
  local attempt="${IRT_ENV_EVIDENCE_DIR}/last-attempt.env"
  local rid status cleanup absent
  [[ -n "${IRT_RUN_ID:-}" ]] || return 1
  [[ -f "$attempt" ]] || return 1
  rid="$(irt_read_evidence_key "$attempt" RUN_ID || true)"
  [[ "$rid" == "$IRT_RUN_ID" ]] || return 1
  status="$(irt_read_evidence_key "$attempt" STATUS || true)"
  cleanup="$(irt_read_evidence_key "$attempt" CLEANUP_OK || true)"
  absent="$(irt_read_evidence_key "$attempt" TEMP_RESOURCES_ABSENT || true)"
  [[ "$status" == "success" || "$status" == "failed" ]] || return 1
  [[ "$cleanup" == "0" || "$cleanup" == "1" ]] || return 1
  [[ "$absent" == "0" || "$absent" == "1" ]] || return 1
  return 0
}

write_emergency_failure_evidence() {
  # Failed last-attempt for the aborted RUN_ID. Never touches last-success.
  irt_ensure_evidence_dirs
  capture_finished_epoch
  IRT_STATUS="failed"
  IRT_ERROR_CODE="${IRT_ERROR_CODE:-EMERGENCY_CLEANUP}"
  local attempt="${IRT_ENV_EVIDENCE_DIR}/last-attempt.env"
  local lines
  mapfile -t lines < <(evidence_lines)
  irt_write_evidence_file "$attempt" "${lines[@]}"
  local hist="${IRT_HISTORY_DIR}/$(date -u +%Y%m%dT%H%M%S%N 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)_$$_${IRT_RUN_ID:-norun}_emergency_failed.env"
  if [[ -e "$hist" ]]; then
    hist="${hist}.$$"
  fi
  irt_write_evidence_file "$hist" "${lines[@]}"
  irt_prune_history
}

print_plan() {
  irt_info "=== Isolated restore-test plan ==="
  irt_info "  environment: ${IRT_ENV}"
  irt_info "  dump dir: ${IRT_DUMP_DIR}"
  irt_info "  dump: ${IRT_DUMP_BASENAME:-'(resolve at run)'}"
  irt_info "  dump age hours: ${IRT_DUMP_AGE_HOURS:-n/a} (max ${IRT_DUMP_MAX_AGE_HOURS})"
  irt_info "  snapshot: private runtime copy (RO mount)"
  irt_info "  pg image: ${IRT_PG_IMAGE} (--pull=never)"
  irt_info "  network: none"
  irt_info "  ports: none"
  irt_info "  pg_restore: --no-owner --no-acl (role-independent integrity)"
  irt_info "  limits: memory=${IRT_DOCKER_MEMORY} cpus=${IRT_DOCKER_CPUS} pids=${IRT_DOCKER_PIDS_LIMIT}"
  irt_info "  evidence: ${IRT_ENV_EVIDENCE_DIR}"
  irt_info "  timeout sec: ${IRT_OVERALL_TIMEOUT_SEC}"
  if [[ "$IRT_DRY_RUN" -eq 1 ]]; then
    irt_info "Mode: DRY-RUN (no Docker mutations, no evidence write)"
  fi
}

# --- orphan reaper / emergency ----------------------------------------------

reap_orphans() {
  IRT_PHASE="reap_orphans"
  local ids id name state created age_h component env_label now
  now="$(date +%s)"
  mapfile -t ids < <(docker ps -aq --filter "label=${IRT_LABEL_COMPONENT}=${IRT_COMPONENT_VALUE}" --filter "label=${IRT_LABEL_ENV}=${IRT_ENV}" 2>/dev/null || true)
  for id in "${ids[@]+"${ids[@]}"}"; do
    [[ -n "$id" ]] || continue
    irt_is_safe_cid "$id" || continue
    name="$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')"
    irt_is_safe_temp_name "$name" || continue
    component="$(irt_container_label "$id" "$IRT_LABEL_COMPONENT")"
    env_label="$(irt_container_label "$id" "$IRT_LABEL_ENV")"
    [[ "$component" == "$IRT_COMPONENT_VALUE" ]] || continue
    [[ "$env_label" == "$IRT_ENV" ]] || continue
    state="$(docker inspect --format '{{.State.Status}}' "$id" 2>/dev/null || echo unknown)"
    case "$state" in
      created|exited|dead) ;;
      *) continue ;; # never reap running
    esac
    created="$(docker inspect --format '{{.Created}}' "$id" 2>/dev/null || true)"
    local created_epoch
    created_epoch="$(date -u -d "$created" +%s 2>/dev/null || echo 0)"
    [[ "$created_epoch" =~ ^[0-9]+$ ]] || continue
    age_h=$(( (now - created_epoch) / 3600 ))
    if (( age_h < IRT_ORPHAN_TTL_HOURS )); then
      continue
    fi
    remove_owned_container_by_ref "$id" "$IRT_ENV" || true
  done
  return 0
}

emergency_cleanup() {
  # Disk-backed recovery for SIGKILL / TimeoutStop. Never depends on killed shell memory.
  # Safe no-op when no current marker/cidfile (normal successful ExecStopPost).
  local marker cidfile cid run_id marker_env started_epoch=""
  local already_finalized=0
  local need_evidence=0
  local cleanup_rc=0
  local evidence_rc=0
  local empty_cleanup_rc=0
  local run_dir resolved expected marker_run

  marker="${IRT_RUNTIME_DIR}/current.env"
  cidfile="${IRT_CIDFILE_ARG:-}"
  run_id="${IRT_RUN_ID_ARG:-}"

  if [[ -f "$marker" ]]; then
    marker_env="$(irt_marker_get "$marker" ENVIRONMENT || true)"
    if [[ -n "$marker_env" && "$marker_env" != "$IRT_ENV" ]]; then
      irt_info "ISOLATED_RESTORE_TEST EMERGENCY skip: marker env mismatch"
      return 0
    fi
    if [[ -z "$cidfile" ]]; then
      cidfile="$(irt_marker_get "$marker" CIDFILE || true)"
    fi
    if [[ -z "$run_id" ]]; then
      run_id="$(irt_marker_get "$marker" RUN_ID || true)"
    fi
    started_epoch="$(irt_marker_get "$marker" STARTED_AT_EPOCH || true)"
  fi

  if [[ -z "$cidfile" || ! -f "$cidfile" ]]; then
    # No in-flight marker → normal completed run (or never started).
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY no-op (no cidfile)"
    return 0
  fi

  if [[ -z "$run_id" || ! "$run_id" =~ $IRT_RUN_ID_RE ]]; then
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY reject: bad RUN_ID"
    return 1
  fi

  resolved="$(irt_realpath "$cidfile" || true)"
  expected="$(irt_realpath "$IRT_RUNTIME_DIR" || true)"
  if [[ -z "$resolved" || -z "$expected" || "$resolved" != "$expected"/* ]]; then
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY reject: cidfile outside runtime"
    return 1
  fi

  # L-01: snapshot/run-dir must come from canonical resolved path only (never marker symlink dirname).
  # Contract: runtime/<run-id>/container.cid — direct child of runtime, not runtime itself / parents / other runs.
  run_dir="$(dirname -- "$resolved")"
  if [[ "$run_dir" == "$expected" \
     || "$(dirname -- "$run_dir")" != "$expected" \
     || "$(basename -- "$run_dir")" != "$run_id" \
     || "$(basename -- "$resolved")" != "container.cid" ]]; then
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY reject: cidfile not runtime/<run-id>/container.cid"
    return 1
  fi

  # Read CID bytes from the resolved file (same object under symlink).
  cid="$(tr -d '[:space:]' <"$resolved" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "$cid" ]]; then
    # Empty cidfile with marker: treat as aborted before docker run; still fix evidence.
    empty_cleanup_rc=0
    IRT_RUN_ID="$run_id"
    if [[ "$started_epoch" =~ ^[1-9][0-9]*$ ]]; then
      IRT_STARTED_EPOCH="$started_epoch"
    else
      IRT_STARTED_EPOCH="$(date +%s)"
    fi
    IRT_TEMP_CID=""
    IRT_TEMP_CONTAINER=""
    IRT_RUN_DIR="$run_dir"
    IRT_SNAPSHOT_PATH="${run_dir}/dump.snapshot"
    # Cleanup leftovers on the canonical run-dir only (L-01).
    if [[ -e "$IRT_SNAPSHOT_PATH" ]]; then
      rm -f -- "$IRT_SNAPSHOT_PATH" || empty_cleanup_rc=1
    fi
    irt_purge_run_dir_files "$run_dir" 1
    verify_cleanup_proof || true
    if [[ "$IRT_TEMP_ABSENT" -ne 1 || "$IRT_SNAPSHOT_ABSENT" -ne 1 ]]; then
      empty_cleanup_rc=1
      IRT_CLEANUP_OK=0
    else
      IRT_CLEANUP_OK=1
    fi
    if irt_attempt_finalized_for_run; then
      if [[ "$empty_cleanup_rc" -eq 0 ]]; then
        rm -f -- "$resolved" 2>/dev/null || true
        if [[ "$cidfile" != "$resolved" ]]; then
          rm -f -- "$cidfile" 2>/dev/null || true
        fi
        irt_purge_run_dir_files "$run_dir" 0
        rmdir "$run_dir" 2>/dev/null || true
        if [[ -d "$run_dir" ]]; then
          empty_cleanup_rc=1
          IRT_CLEANUP_OK=0
          IRT_TEMP_ABSENT=0
          irt_info "ISOLATED_RESTORE_TEST EMERGENCY incomplete empty-cid cleanup RUN_ID=${IRT_RUN_ID}"
          return 1
        fi
        [[ -f "$marker" ]] && rm -f -- "$marker" 2>/dev/null || true
        irt_info "ISOLATED_RESTORE_TEST EMERGENCY no-op (empty cid; RUN_ID already finalized)"
        return 0
      fi
      irt_info "ISOLATED_RESTORE_TEST EMERGENCY incomplete cleanup for finalized empty-cid RUN_ID=${IRT_RUN_ID}"
      return 1
    fi
    IRT_ERROR_CODE="EMERGENCY_CLEANUP"
    if ! write_emergency_failure_evidence; then
      return 1
    fi
    if [[ "$empty_cleanup_rc" -eq 0 ]]; then
      rm -f -- "$resolved" 2>/dev/null || true
      if [[ "$cidfile" != "$resolved" ]]; then
        rm -f -- "$cidfile" 2>/dev/null || true
      fi
      irt_purge_run_dir_files "$run_dir" 0
      rmdir "$run_dir" 2>/dev/null || true
      if [[ -d "$run_dir" ]]; then
        IRT_CLEANUP_OK=0
        IRT_TEMP_ABSENT=0
        irt_info "ISOLATED_RESTORE_TEST EMERGENCY incomplete empty-cid cleanup RUN_ID=${IRT_RUN_ID}"
        return 1
      fi
      [[ -f "$marker" ]] && rm -f -- "$marker" 2>/dev/null || true
      irt_info "ISOLATED_RESTORE_TEST EMERGENCY failure evidence for empty-cid RUN_ID=${IRT_RUN_ID}"
      return 0
    fi
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY incomplete empty-cid cleanup RUN_ID=${IRT_RUN_ID}"
    return 1
  fi

  irt_is_safe_cid "$cid" || {
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY reject: bad cid format"
    return 1
  }

  IRT_TEMP_CID="$cid"
  IRT_RUN_ID="$run_id"
  IRT_TEMP_CONTAINER="bot-tv-rt-staging-${IRT_RUN_ID}"
  IRT_RUN_DIR="$run_dir"
  if [[ "$started_epoch" =~ ^[1-9][0-9]*$ ]]; then
    IRT_STARTED_EPOCH="$started_epoch"
  else
    IRT_STARTED_EPOCH="$(date +%s)"
  fi
  IRT_DUMP_BASENAME=""
  IRT_DUMP_SHA=""
  IRT_DUMP_SIZE=0
  IRT_DUMP_AGE_HOURS=0
  IRT_DUMP_MTIME_UTC=""
  IRT_SNAPSHOT_PATH="${run_dir}/dump.snapshot"

  if irt_attempt_finalized_for_run; then
    already_finalized=1
  else
    need_evidence=1
  fi

  # Cleanup first (before evidence).
  if irt_container_exists "$cid"; then
    if ! remove_owned_container_by_ref "$cid" "$IRT_ENV" "$IRT_RUN_ID"; then
      cleanup_rc=1
    fi
  fi
  if [[ -e "${IRT_SNAPSHOT_PATH}" ]]; then
    rm -f -- "$IRT_SNAPSHOT_PATH" || cleanup_rc=1
  fi
  irt_purge_run_dir_files "$run_dir" 1
  # Keep resolved/marker cidfile until cleanup_ok && evidence_ok (retry safety).

  verify_cleanup_proof || true
  if [[ "$IRT_TEMP_ABSENT" -ne 1 || "$IRT_SNAPSHOT_ABSENT" -ne 1 ]]; then
    cleanup_rc=1
    IRT_CLEANUP_OK=0
  else
    IRT_CLEANUP_OK=1
  fi

  IRT_ERROR_CODE="EMERGENCY_CLEANUP"

  if [[ "$already_finalized" -eq 1 ]]; then
    # Same RUN_ID already fully recorded by main finalizer — do not rewrite evidence.
    if [[ "$cleanup_rc" -eq 0 ]]; then
      rm -f -- "$resolved" 2>/dev/null || true
      if [[ "$cidfile" != "$resolved" ]]; then
        rm -f -- "$cidfile" 2>/dev/null || true
      fi
      irt_purge_run_dir_files "$run_dir" 0
      rmdir "$run_dir" 2>/dev/null || true
      if [[ -d "$run_dir" ]]; then
        irt_info "ISOLATED_RESTORE_TEST EMERGENCY incomplete cleanup for finalized RUN_ID=${IRT_RUN_ID}"
        return 1
      fi
      [[ -f "$marker" ]] && rm -f -- "$marker" 2>/dev/null || true
      irt_info "ISOLATED_RESTORE_TEST EMERGENCY no-op (RUN_ID=${IRT_RUN_ID} already finalized)"
      return 0
    fi
    # Cleanup still incomplete: keep marker/cidfile for retry; do not clobber evidence.
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY incomplete cleanup for finalized RUN_ID=${IRT_RUN_ID}"
    return 1
  fi

  if [[ "$need_evidence" -eq 1 ]]; then
    if ! write_emergency_failure_evidence; then
      evidence_rc=1
    fi
  fi

  # Remove marker/cidfile only when resources are gone AND evidence is fixed.
  if [[ "$cleanup_rc" -eq 0 && "$evidence_rc" -eq 0 ]]; then
    rm -f -- "$resolved" 2>/dev/null || true
    if [[ "$cidfile" != "$resolved" ]]; then
      # Drop the marker-side symlink path; never use its dirname for snapshot cleanup.
      rm -f -- "$cidfile" 2>/dev/null || true
    fi
    irt_purge_run_dir_files "$run_dir" 0
    rmdir "$run_dir" 2>/dev/null || true
    if [[ -d "$run_dir" ]]; then
      cleanup_rc=1
      IRT_CLEANUP_OK=0
      IRT_TEMP_ABSENT=0
    else
      if [[ -f "$marker" ]]; then
        marker_run="$(irt_marker_get "$marker" RUN_ID || true)"
        if [[ "$marker_run" == "$IRT_RUN_ID" ]]; then
          rm -f -- "$marker" 2>/dev/null || true
        fi
      fi
    fi
  fi

  if [[ "$cleanup_rc" -ne 0 || "$evidence_rc" -ne 0 ]]; then
    irt_info "ISOLATED_RESTORE_TEST EMERGENCY incomplete env=${IRT_ENV} run=${IRT_RUN_ID} cleanup=${IRT_CLEANUP_OK} evidence_rc=${evidence_rc}"
    return 1
  fi

  irt_info "ISOLATED_RESTORE_TEST EMERGENCY cleanup ok env=${IRT_ENV} run=${IRT_RUN_ID} durationSec=$(irt_duration_sec)"
  return 0
}

# --- main run ---------------------------------------------------------------

run_test() {
  IRT_STARTED_EPOCH="$(date +%s)"
  install_lifecycle_traps

  if [[ "$IRT_DRY_RUN" -eq 1 ]]; then
    select_dump
    print_plan
    irt_info "ISOLATED_RESTORE_TEST DRY_RUN OK env=${IRT_ENV} dump=${IRT_DUMP_BASENAME}"
    IRT_EXIT_CODE=0
    IRT_WORK_OK=1
    IRT_CLEANUP_OK=1
    IRT_TEMP_ABSENT=1
    IRT_SNAPSHOT_ABSENT=1
    IRT_SKIP_EVIDENCE=1
    exit 0
  fi

  acquire_lock
  select_dump
  print_plan
  prepare_run_identity
  create_dump_snapshot
  verify_snapshot_before_mount
  assert_image_present
  capture_forbidden_pre

  # Best-effort stopped orphan sweep before creating a new container.
  reap_orphans || true

  start_temp_postgres
  run_restore
  run_integrity_checks
  verify_snapshot_after_restore
  # bot-TV: no Prisma migration-proof contour
  assert_forbidden_unchanged

  IRT_WORK_OK=1
  # Harness-only deterministic pause so SIGTERM after work_ok cannot race past exit 0.
  if [[ "${IRT_TEST_PAUSE_AFTER_WORK_OK:-0}" == "1" ]]; then
    : >"${IRT_RUNTIME_DIR}/.pause-after-work-ok"
    while true; do
      set +e
      irt_interruptible_run sleep 3600
      set -e
      if [[ "${IRT_SIGNAL_RECEIVED:-0}" -eq 1 ]]; then
        exit 50
      fi
    done
  fi
  # EXIT finalizer performs cleanup + evidence + exit code.
  exit 0
}

main() {
  parse_args "$@"
  if [[ "$IRT_HELP" -eq 1 ]]; then
    usage
    exit 0
  fi
  [[ -n "$IRT_ENV_ARG" ]] || irt_die "--environment is required"
  irt_resolve_environment "$IRT_ENV_ARG"
  # sha256sum preferred; shasum fallback inside helper.
  if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    irt_die "sha256sum or shasum required"
  fi

  case "$IRT_MODE" in
    emergency-cleanup)
      # flock not required: ExecStopPost must work after SIGKILL without lock tooling gaps.
      irt_require_commands docker date stat
      irt_ensure_evidence_dirs
      if emergency_cleanup; then
        exit 0
      fi
      exit 50
      ;;
    reap-orphans)
      irt_require_commands docker date stat
      irt_ensure_evidence_dirs
      reap_orphans
      exit 0
      ;;
    run)
      irt_require_commands docker flock date stat
      run_test
      ;;
    *)
      irt_die "unknown mode"
      ;;
  esac
}

main "$@"
