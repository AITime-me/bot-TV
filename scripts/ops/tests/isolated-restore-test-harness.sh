#!/usr/bin/env bash
# Linux/Git-Bash behavioral harness for isolated-restore-test failure paths.
# Uses fake docker; never touches real Docker, dumps, or working DBs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT="${ROOT}/scripts/ops/isolated-restore-test.sh"
FAKE_SRC="${ROOT}/scripts/ops/lib/fake-docker-irt.sh"
PASS=0
FAIL=0
SKIP=0
RESULTS=()

note() { RESULTS+=("$1"); }

ok() { PASS=$((PASS + 1)); note "PASS $1"; echo "PASS $1"; }
bad() { FAIL=$((FAIL + 1)); note "FAIL $1 — $2"; echo "FAIL $1 — $2" >&2; }
skip() { SKIP=$((SKIP + 1)); note "SKIP $1 — $2"; echo "SKIP $1 — $2"; }

require_flock() {
  command -v flock >/dev/null 2>&1
}

setup_case() {
  CASE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/irt-harness.XXXXXX")"
  BIN="${CASE_DIR}/bin"
  STATE="${CASE_DIR}/docker-state"
  EVIDENCE="${CASE_DIR}/evidence"
  DUMPS="${CASE_DIR}/dumps"
  mkdir -p "$BIN" "$STATE" "$EVIDENCE" "$DUMPS"
  cp -- "$FAKE_SRC" "${BIN}/docker"
  chmod +x "${BIN}/docker"
  # Always shadow flock with a controllable test double so lock contention is
  # executable on hosts without util-linux flock (Windows Git Bash).
  cat >"${BIN}/flock" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) shift ;;
    -u) shift; exit 0 ;;
    *)
      if [[ "${IRT_FAKE_FLOCK_BUSY:-0}" == "1" ]]; then
        exit 1
      fi
      exit 0
      ;;
  esac
done
exit 0
EOF
  chmod +x "${BIN}/flock"
  export PATH="${BIN}:${PATH}"
  export FAKE_DOCKER_STATE="$STATE"
  export FAKE_DOCKER_MODE="${1:-ok}"
  export IRT_DUMP_DIR_OVERRIDE="$DUMPS"
  export IRT_SKIP_FORBIDDEN_CHECK="${IRT_SKIP_FORBIDDEN_CHECK:-1}"
}

make_dump() {
  local name="${1:-20260726T120000Z_testdump.dump}"
  local path="${DUMPS}/${name}"
  printf 'FAKE-PG-DUMP-%s' "$name" >"$path"
  # fresh mtime
  touch "$path"
  printf '%s' "$path"
}

seed_success() {
  local env_name="$1"
  local run_id="${2:-aaaaaaaaaaaaaaaa}"
  mkdir -p "${EVIDENCE}/${env_name}/history" "${EVIDENCE}/${env_name}/runtime"
  cat >"${EVIDENCE}/${env_name}/last-success.env" <<EOF
SCHEMA_VERSION=1
ENVIRONMENT=${env_name}
STATUS=success
ERROR_CODE=
STARTED_AT_UTC=2026-07-20T00:00:00Z
FINISHED_AT_UTC=2026-07-20T00:05:00Z
DURATION_SEC=300
RUN_ID=${run_id}
DUMP_BASENAME=20260720T000000Z_old.dump
DUMP_MTIME_UTC=2026-07-20T00:00:00Z
DUMP_SIZE_BYTES=10
DUMP_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
DUMP_AGE_HOURS=1
PG_IMAGE=postgres:17-alpine
TEMP_CONTAINER=
TEMP_CID=
TEMP_PG_STARTED=0
RESTORE_OK=1
SQL_CONNECT_OK=1
USER_SCHEMA_COUNT=1
USER_TABLE_COUNT=5
INTEGRITY_OK=1
CLEANUP_OK=1
TEMP_RESOURCES_ABSENT=1
SNAPSHOT_ABSENT=1
EOF
  cp -- "${EVIDENCE}/${env_name}/last-success.env" "${EVIDENCE}/${env_name}/last-attempt.env"
}

read_key() {
  local file="$1" key="$2"
  grep -E "^${key}=" "$file" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

run_irt() {
  local env_name="${1:-staging}"
  shift || true
  bash "$SCRIPT" --environment "$env_name" --evidence-root "$EVIDENCE" "$@"
}

# Background signal tests must target the restore-test process that owns INT/TERM
# traps (same contract as systemd KillMode on the unit MainPID).
# `run_irt &` backgrounds a *wrapper* function shell without traps; SIGTERM then
# yields harness rc=143, leaves the real script orphaned, and skips finalizer.
# `exec` replaces that wrapper in-place so $! is the trapped restore-test PID.
run_irt_bg() {
  local env_name="${1:-staging}"
  shift || true
  exec bash "$SCRIPT" --environment "$env_name" --evidence-root "$EVIDENCE" "$@"
}

# --- scenarios --------------------------------------------------------------

scenario_dump_missing() {
  setup_case ok
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 10 ]]; then ok "dump_missing"; else bad "dump_missing" "rc=$rc"; fi
}

scenario_dump_stale() {
  setup_case ok
  local p
  p="$(make_dump)"
  # age > 36h
  if touch -d '40 hours ago' "$p" 2>/dev/null || touch -t 202601010101 "$p" 2>/dev/null; then
    set +e
    run_irt staging
    local rc=$?
    set -e
    if [[ "$rc" -eq 10 ]]; then ok "dump_stale"; else bad "dump_stale" "rc=$rc"; fi
  else
    skip "dump_stale" "touch -d unavailable"
  fi
}

scenario_dump_unreadable() {
  setup_case ok
  local p
  p="$(make_dump)"
  chmod 000 "$p" || true
  if [[ -r "$p" ]]; then
    chmod 644 "$p" 2>/dev/null || true
    skip "dump_unreadable" "chmod 000 ineffective on this host"
    return
  fi
  set +e
  run_irt staging
  local rc=$?
  set -e
  chmod 644 "$p" 2>/dev/null || true
  if [[ "$rc" -eq 10 ]]; then ok "dump_unreadable"; else bad "dump_unreadable" "rc=$rc"; fi
}

scenario_noimage() {
  setup_case noimage
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 20 ]]; then ok "noimage"; else bad "noimage" "rc=$rc"; fi
}

scenario_notready() {
  setup_case notready
  make_dump >/dev/null
  export IRT_PG_READY_TIMEOUT_SEC=2
  set +e
  run_irt staging
  local rc=$?
  set -e
  unset IRT_PG_READY_TIMEOUT_SEC
  if [[ "$rc" -eq 20 ]]; then ok "notready"; else bad "notready" "rc=$rc"; fi
}

scenario_restorefail() {
  setup_case restorefail
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 30 ]]; then ok "restorefail"; else bad "restorefail" "rc=$rc"; fi
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  [[ -f "$attempt" ]] || { bad "restorefail_evidence" "no last-attempt"; return; }
  [[ "$(read_key "$attempt" ERROR_CODE)" == "PG_RESTORE_FAILED" ]] \
    && ok "restorefail_code" \
    || bad "restorefail_code" "ERROR_CODE=$(read_key "$attempt" ERROR_CODE)"
  [[ "$(read_key "$attempt" CLEANUP_OK)" == "1" ]] || bad "restorefail_cleanup" "CLEANUP_OK!=1"
  [[ "$(read_key "$attempt" TEMP_RESOURCES_ABSENT)" == "1" ]] || bad "restorefail_absent" "absent!=1"
  local run_id diag_rel diag_path latest
  run_id="$(read_key "$attempt" RUN_ID)"
  diag_rel="$(read_key "$attempt" PG_RESTORE_ERROR_LOG)"
  if [[ -n "$run_id" && "$diag_rel" == "history/pg_restore_${run_id}.error.log" ]]; then
    ok "restorefail_diag_link"
  else
    bad "restorefail_diag_link" "RUN_ID=$run_id PG_RESTORE_ERROR_LOG=$diag_rel"
  fi
  diag_path="${EVIDENCE}/staging/${diag_rel}"
  latest="${EVIDENCE}/staging/last-pg-restore-error.log"
  if [[ -f "$diag_path" && -f "$latest" ]]; then
    ok "restorefail_diag_files"
  else
    bad "restorefail_diag_files" "missing diag path=$diag_path latest=$latest"
  fi
  if grep -Eq 'missing_table|relation' "$diag_path" 2>/dev/null; then
    ok "restorefail_diag_content"
  else
    bad "restorefail_diag_content" "expected pg_restore error text"
  fi
  if grep -Eq 'super-secret-token-do-not-leak|postgres://user:secret@|do-not-leak-9f3a' "$diag_path" "$latest" 2>/dev/null; then
    bad "restorefail_diag_no_secret" "secret leaked into diagnostic evidence"
  else
    ok "restorefail_diag_no_secret"
  fi
  if grep -Eq 'PGPASSWORD=<redacted>|DATABASE_URL=<redacted>' "$diag_path" 2>/dev/null; then
    ok "restorefail_diag_redacted"
  else
    bad "restorefail_diag_redacted" "expected redacted markers"
  fi
  # Mode bits: evidence files must not be group/world readable when chmod works.
  # Git Bash/Windows often cannot enforce Unix 0600 on NTFS — skip mode assert there.
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      skip "restorefail_diag_mode" "chmod 0600 not reliable on this host"
      ;;
    *)
      local mode
      mode="$(stat -c '%a' "$diag_path" 2>/dev/null || echo unknown)"
      if [[ "$mode" == "600" ]]; then
        ok "restorefail_diag_mode"
      else
        bad "restorefail_diag_mode" "mode=$mode (expected 600)"
      fi
      ;;
  esac
  if [[ -d "${STATE}/containers" ]] && compgen -G "${STATE}/containers/*" >/dev/null; then
    bad "restorefail_docker_cleaned" "container still present"
  else
    ok "restorefail_docker_cleaned"
  fi
}

scenario_foreign_owner_ok() {
  # Dump objects owned by role tvoe_vremya (absent in clean container).
  # Must succeed via --no-owner --no-acl without creating that role.
  setup_case foreign-owner
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then ok "foreign_owner_ok"; else bad "foreign_owner_ok" "rc=$rc"; fi
  local success="${EVIDENCE}/staging/last-success.env"
  [[ -f "$success" ]] || { bad "foreign_owner_success_evidence" "no last-success"; return; }
  [[ "$(read_key "$success" STATUS)" == "success" ]] \
    && ok "foreign_owner_status" \
    || bad "foreign_owner_status" "STATUS=$(read_key "$success" STATUS)"
  [[ "$(read_key "$success" CLEANUP_OK)" == "1" ]] || bad "foreign_owner_cleanup" "CLEANUP_OK!=1"
  [[ "$(read_key "$success" TEMP_RESOURCES_ABSENT)" == "1" ]] || bad "foreign_owner_absent" "absent!=1"
  [[ -z "$(read_key "$success" PG_RESTORE_ERROR_LOG)" ]] \
    && ok "foreign_owner_no_diag" \
    || bad "foreign_owner_no_diag" "PG_RESTORE_ERROR_LOG set on success"
  if [[ -f "${EVIDENCE}/staging/last-pg-restore-error.log" ]]; then
    bad "foreign_owner_no_active_diag" "active error log present after success"
  else
    ok "foreign_owner_no_active_diag"
  fi
}

scenario_diag_cleared_on_success() {
  # Failed restore leaves active diagnostic; following success must clear it.
  # Keep one CASE_DIR so evidence root persists across the fail→success sequence.
  setup_case restorefail
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  [[ "$rc" -eq 30 ]] || { bad "diag_clear_fail_rc" "rc=$rc"; return; }
  local latest="${EVIDENCE}/staging/last-pg-restore-error.log"
  [[ -f "$latest" ]] || { bad "diag_clear_precondition" "expected active error log after fail"; return; }
  local fail_run
  fail_run="$(read_key "${EVIDENCE}/staging/last-attempt.env" RUN_ID)"
  local hist_diag="${EVIDENCE}/staging/history/pg_restore_${fail_run}.error.log"
  [[ -f "$hist_diag" ]] || { bad "diag_clear_history_kept_pre" "history diag missing"; return; }
  local hist_before
  hist_before="$(cat "$hist_diag")"

  export FAKE_DOCKER_MODE="ok"
  make_dump >/dev/null
  set +e
  run_irt staging
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then ok "diag_clear_success_rc"; else bad "diag_clear_success_rc" "rc=$rc"; fi
  if [[ -f "$latest" ]]; then
    bad "diag_clear_active_gone" "active error log still present after success"
  else
    ok "diag_clear_active_gone"
  fi
  if [[ ! -f "$hist_diag" ]]; then
    bad "diag_clear_history_retained" "history diagnostic missing after success"
  elif [[ "$(cat "$hist_diag")" != "$hist_before" ]]; then
    bad "diag_clear_history_retained" "history diagnostic content changed"
  else
    ok "diag_clear_history_retained"
  fi
  local success="${EVIDENCE}/staging/last-success.env"
  local success_run
  success_run="$(read_key "$success" RUN_ID)"
  if [[ -n "$success_run" && "$success_run" != "$fail_run" ]]; then
    ok "diag_clear_success_run_id"
  else
    bad "diag_clear_success_run_id" "success_run=$success_run fail_run=$fail_run"
  fi
  [[ -z "$(read_key "$success" PG_RESTORE_ERROR_LOG)" ]] \
    && ok "diag_clear_success_key_empty" \
    || bad "diag_clear_success_key_empty" "PG_RESTORE_ERROR_LOG=$(read_key "$success" PG_RESTORE_ERROR_LOG)"
  if [[ -d "${STATE}/containers" ]] && compgen -G "${STATE}/containers/*" >/dev/null; then
    bad "diag_clear_cleanup" "container still present"
  else
    ok "diag_clear_cleanup"
  fi
}

scenario_restorefail_diag_size_cap() {
  setup_case restorefail-huge
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 30 ]]; then ok "diag_size_rc"; else bad "diag_size_rc" "rc=$rc"; fi
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  local run_id diag_rel diag_path latest size latest_size
  run_id="$(read_key "$attempt" RUN_ID)"
  diag_rel="$(read_key "$attempt" PG_RESTORE_ERROR_LOG)"
  if [[ "$diag_rel" == "history/pg_restore_${run_id}.error.log" ]]; then
    ok "diag_size_link"
  else
    bad "diag_size_link" "PG_RESTORE_ERROR_LOG=$diag_rel"
  fi
  diag_path="${EVIDENCE}/staging/${diag_rel}"
  latest="${EVIDENCE}/staging/last-pg-restore-error.log"
  [[ -f "$diag_path" && -f "$latest" ]] || { bad "diag_size_files" "missing"; return; }
  size="$(wc -c <"$diag_path" | tr -d '[:space:]')"
  latest_size="$(wc -c <"$latest" | tr -d '[:space:]')"
  if [[ "$size" =~ ^[0-9]+$ ]] && (( size <= 16384 )); then
    ok "diag_size_history_cap"
  else
    bad "diag_size_history_cap" "size=$size"
  fi
  if [[ "$latest_size" =~ ^[0-9]+$ ]] && (( latest_size <= 16384 )); then
    ok "diag_size_active_cap"
  else
    bad "diag_size_active_cap" "size=$latest_size"
  fi
  if grep -Fq '[truncated to 16384 bytes]' "$diag_path"; then
    ok "diag_size_trailer"
  else
    bad "diag_size_trailer" "missing truncation trailer"
  fi
  if grep -Eq 'super-secret-token-do-not-leak|do-not-leak-9f3a' "$diag_path" "$latest" 2>/dev/null; then
    bad "diag_size_no_secret" "secret leaked"
  else
    ok "diag_size_no_secret"
  fi
}

scenario_pg_restore_flags_contract() {
  # Prove fake pg_restore rejects missing flags; production path supplies all three.
  setup_case foreign-owner
  local out rc
  set +e
  out="$("${BIN}/docker" exec fake-cid pg_restore -U postgres -d restore_test --exit-on-error /dump 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]] && echo "$out" | grep -q 'tvoe_vremya'; then
    ok "flags_missing_both_owners"
  else
    bad "flags_missing_both_owners" "rc=$rc out=$out"
  fi
  set +e
  out="$("${BIN}/docker" exec fake-cid pg_restore -U postgres -d restore_test --exit-on-error --no-owner /dump 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]] && echo "$out" | grep -q 'tvoe_vremya'; then
    ok "flags_missing_no_acl"
  else
    bad "flags_missing_no_acl" "rc=$rc out=$out"
  fi
  set +e
  out="$("${BIN}/docker" exec fake-cid pg_restore -U postgres -d restore_test --exit-on-error --no-acl /dump 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]] && echo "$out" | grep -q 'tvoe_vremya'; then
    ok "flags_missing_no_owner"
  else
    bad "flags_missing_no_owner" "rc=$rc out=$out"
  fi
  set +e
  out="$("${BIN}/docker" exec fake-cid pg_restore -U postgres -d restore_test --no-owner --no-acl /dump 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]] && echo "$out" | grep -q 'tvoe_vremya'; then
    ok "flags_missing_exit_on_error"
  else
    bad "flags_missing_exit_on_error" "rc=$rc out=$out"
  fi
  set +e
  out="$("${BIN}/docker" exec fake-cid pg_restore -U postgres -d restore_test --exit-on-error --no-owner --no-acl /dump 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then
    ok "flags_all_present_ok"
  else
    bad "flags_all_present_ok" "rc=$rc out=$out"
  fi
  # Production script path must pass the full flag set (enforced by fake on success).
  make_dump >/dev/null
  set +e
  run_irt staging
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then ok "flags_production_path"; else bad "flags_production_path" "rc=$rc"; fi
}

scenario_emergency_purges_pg_restore_log() {
  # SIGKILL/ExecStopPost path must remove raw runtime pg_restore.log and the run-dir.
  setup_case ok
  local run_id="cafebabecafebabe"
  local cid="abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
  local run_dir="${EVIDENCE}/staging/runtime/${run_id}"
  mkdir -p "$run_dir" "${STATE}/containers/${cid}"
  printf 'bot-tv-rt-staging-%s\n' "$run_id" >"${STATE}/containers/${cid}/name"
  printf 'running\n' >"${STATE}/containers/${cid}/status"
  cat >"${STATE}/containers/${cid}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=${run_id}
EOF
  local cidfile="${run_dir}/container.cid"
  printf '%s' "$cid" >"$cidfile"
  printf 'RAW PGPASSWORD=super-secret-token-do-not-leak\n' >"${run_dir}/pg_restore.log"
  : >"${run_dir}/schemas.count"
  : >"${run_dir}/tables.count"
  : >"${run_dir}/dump.snapshot.partial"
  cat >"${EVIDENCE}/staging/runtime/current.env" <<EOF
RUN_ID=${run_id}
CIDFILE=${cidfile}
ENVIRONMENT=staging
STARTED_AT_EPOCH=$(date +%s)
EOF
  [[ -f "${run_dir}/pg_restore.log" ]] || { bad "emergency_log_precondition" "log missing"; return; }
  set +e
  bash "$SCRIPT" --emergency-cleanup --environment staging --evidence-root "$EVIDENCE"
  local rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then ok "emergency_log_rc"; else bad "emergency_log_rc" "rc=$rc"; fi
  if [[ -e "${run_dir}/pg_restore.log" ]]; then
    bad "emergency_log_gone" "pg_restore.log remains"
  else
    ok "emergency_log_gone"
  fi
  if [[ -d "$run_dir" ]]; then
    bad "emergency_rundir_gone" "run-dir remains"
  else
    ok "emergency_rundir_gone"
  fi
  if [[ -d "${STATE}/containers/${cid}" ]]; then
    bad "emergency_log_container_gone" "container remains"
  else
    ok "emergency_log_container_gone"
  fi
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" TEMP_RESOURCES_ABSENT)" == "1" ]] \
    && ok "emergency_log_absent_flag" \
    || bad "emergency_log_absent_flag" "TEMP_RESOURCES_ABSENT!=1"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" CLEANUP_OK)" == "1" ]] \
    && ok "emergency_log_cleanup_ok" \
    || bad "emergency_log_cleanup_ok" "CLEANUP_OK!=1"
}

scenario_integrityfail() {
  setup_case integrityfail
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 40 ]]; then ok "integrityfail"; else bad "integrityfail" "rc=$rc"; fi
}

scenario_term() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      skip "term_interrupt" "SIGTERM to blocked child unreliable on Git Bash"
      return
      ;;
  esac
  setup_case hang-after-run
  make_dump >/dev/null
  set +e
  run_irt_bg staging &
  local pid=$!
  local waited=0
  # Wait until fake docker has created a container (pg_isready hang phase).
  while (( waited < 20 )); do
    if compgen -G "${STATE}/containers/*" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  kill -TERM "$pid" 2>/dev/null || true
  wait "$pid"
  local rc=$?
  set -e
  if [[ "$rc" -eq 50 ]]; then ok "term_interrupt"; else bad "term_interrupt" "rc=$rc"; fi
  if [[ -d "${STATE}/containers" ]] && compgen -G "${STATE}/containers/*" >/dev/null; then
    bad "term_cleanup" "container still present"
  else
    ok "term_cleanup"
  fi
  # Runtime leftovers must be gone after finalizer cleanup.
  if compgen -G "${EVIDENCE}/staging/runtime/*/dump.snapshot" >/dev/null 2>&1 \
    || compgen -G "${EVIDENCE}/staging/runtime/*/container.cid" >/dev/null 2>&1 \
    || [[ -f "${EVIDENCE}/staging/runtime/current.env" ]]; then
    bad "term_runtime_cleanup" "snapshot/cidfile/current.env remain"
  else
    ok "term_runtime_cleanup"
  fi
  # Interrupted run must leave failed attempt evidence when storage is writable.
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  if [[ -f "$attempt" ]] \
    && [[ "$(read_key "$attempt" STATUS)" == "failed" ]] \
    && [[ "$(read_key "$attempt" ERROR_CODE)" == "INTERRUPTED" ]]; then
    ok "term_evidence_failed"
  else
    bad "term_evidence_failed" "expected STATUS=failed ERROR_CODE=INTERRUPTED"
  fi
  if [[ -f "${EVIDENCE}/staging/last-success.env" ]]; then
    bad "term_no_success" "last-success written on interrupt"
  else
    ok "term_no_success"
  fi
}

assert_term_cleanup_residue() {
  local prefix="$1"
  if [[ -d "${STATE}/containers" ]] && compgen -G "${STATE}/containers/*" >/dev/null; then
    bad "${prefix}_cleanup" "container still present"
  else
    ok "${prefix}_cleanup"
  fi
  if compgen -G "${EVIDENCE}/staging/runtime/*/dump.snapshot" >/dev/null 2>&1 \
    || compgen -G "${EVIDENCE}/staging/runtime/*/container.cid" >/dev/null 2>&1 \
    || [[ -f "${EVIDENCE}/staging/runtime/current.env" ]]; then
    bad "${prefix}_runtime_cleanup" "snapshot/cidfile/current.env remain"
  else
    ok "${prefix}_runtime_cleanup"
  fi
  if [[ -f "${EVIDENCE}/staging/last-success.env" ]]; then
    bad "${prefix}_no_success" "last-success written on interrupt/fail"
  else
    ok "${prefix}_no_success"
  fi
}

scenario_term_during_restore() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      skip "term_during_restore" "SIGTERM to blocked child unreliable on Git Bash"
      return
      ;;
  esac
  setup_case hang-on-restore
  make_dump >/dev/null
  set +e
  run_irt_bg staging &
  local pid=$!
  local waited=0
  local hang_marker="${STATE}/pg_restore.hanging"
  # Deterministic barrier: fake docker touches this only after entering hung pg_restore.
  while (( waited < 60 )); do
    if [[ -f "$hang_marker" ]]; then
      break
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  if [[ ! -f "$hang_marker" ]]; then
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    set -e
    bad "term_during_restore_rc" "pg_restore hang marker never appeared"
    return
  fi
  kill -TERM "$pid" 2>/dev/null || true
  wait "$pid"
  local rc=$?
  set -e
  if [[ "$rc" -eq 50 ]]; then ok "term_during_restore_rc"; else bad "term_during_restore_rc" "rc=$rc"; fi
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  if [[ -f "$attempt" ]] \
    && [[ "$(read_key "$attempt" ERROR_CODE)" == "INTERRUPTED" ]] \
    && [[ "$(read_key "$attempt" STATUS)" == "failed" ]]; then
    ok "term_during_restore_evidence"
  else
    bad "term_during_restore_evidence" "expected failed/INTERRUPTED last-attempt"
  fi
  assert_term_cleanup_residue "term_during_restore"
  # Parent script exited; no harness-owned background jobs should remain.
  if [[ -n "$(jobs -p 2>/dev/null || true)" ]]; then
    bad "term_during_restore_jobs" "background jobs remain: $(jobs -p)"
  else
    ok "term_during_restore_jobs"
  fi
}

scenario_term_after_work_ok() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*)
      skip "term_after_work_ok" "SIGTERM to blocked child unreliable on Git Bash"
      return
      ;;
  esac
  setup_case ok
  make_dump >/dev/null
  export IRT_TEST_PAUSE_AFTER_WORK_OK=1
  set +e
  run_irt_bg staging &
  local pid=$!
  local waited=0
  local pause_marker="${EVIDENCE}/staging/runtime/.pause-after-work-ok"
  while (( waited < 60 )); do
    if [[ -f "$pause_marker" ]]; then
      break
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  unset IRT_TEST_PAUSE_AFTER_WORK_OK
  if [[ ! -f "$pause_marker" ]]; then
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    set -e
    bad "term_after_work_ok_rc" "pause marker never appeared"
    return
  fi
  kill -TERM "$pid" 2>/dev/null || true
  wait "$pid"
  local rc=$?
  set -e
  if [[ "$rc" -eq 50 ]]; then ok "term_after_work_ok_rc"; else bad "term_after_work_ok_rc" "rc=$rc (must not be 0)"; fi
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  if [[ -f "$attempt" ]] \
    && [[ "$(read_key "$attempt" STATUS)" == "failed" ]] \
    && [[ "$(read_key "$attempt" ERROR_CODE)" == "INTERRUPTED" ]]; then
    ok "term_after_work_ok_evidence"
  else
    bad "term_after_work_ok_evidence" "expected STATUS=failed ERROR_CODE=INTERRUPTED"
  fi
  assert_term_cleanup_residue "term_after_work_ok"
}

scenario_child_signal_death_one() {
  local mode="$1"
  local tag="$2"
  setup_case "$mode"
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 50 ]]; then ok "child_signal_death_${tag}_rc"; else bad "child_signal_death_${tag}_rc" "rc=$rc"; fi
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  local err status
  err="$(read_key "$attempt" ERROR_CODE 2>/dev/null || true)"
  status="$(read_key "$attempt" STATUS 2>/dev/null || true)"
  if [[ "$status" == "failed" && -n "$err" && "$err" != "INTERRUPTED" ]]; then
    ok "child_signal_death_${tag}_code"
  else
    bad "child_signal_death_${tag}_code" "STATUS=$status ERROR_CODE=$err (must be failed, not INTERRUPTED)"
  fi
  if [[ "$err" == "PG_RESTORE_FAILED" ]]; then
    ok "child_signal_death_${tag}_phase"
  else
    bad "child_signal_death_${tag}_phase" "expected PG_RESTORE_FAILED got=$err"
  fi
  if [[ -d "${STATE}/containers" ]] && compgen -G "${STATE}/containers/*" >/dev/null; then
    bad "child_signal_death_${tag}_cleanup" "container still present"
  else
    ok "child_signal_death_${tag}_cleanup"
  fi
  if [[ -f "${EVIDENCE}/staging/last-success.env" ]]; then
    bad "child_signal_death_${tag}_no_success" "last-success written on child signal death"
  else
    ok "child_signal_death_${tag}_no_success"
  fi
}

scenario_child_signal_death() {
  # Child exit 137/143 without parent SIGTERM must stay phase-failed (M3), not INTERRUPTED.
  scenario_child_signal_death_one restore-child-137 137
  scenario_child_signal_death_one restore-child-143 143
}

scenario_rmfail() {
  setup_case rmfail
  make_dump >/dev/null
  set +e
  run_irt staging
  local rc=$?
  set -e
  # restore may succeed then cleanup fail → 50
  if [[ "$rc" -eq 50 ]]; then ok "partial_cleanup"; else bad "partial_cleanup" "rc=$rc"; fi
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  [[ "$(read_key "$attempt" CLEANUP_OK)" == "0" ]] || bad "partial_cleanup_flag" "CLEANUP_OK not 0"
  [[ ! -f "${EVIDENCE}/staging/last-success.env" ]] || [[ "$(read_key "${EVIDENCE}/staging/last-success.env" STATUS)" != "success" ]] \
    || true
  # ensure last-success not created as success from this run
  if [[ -f "${EVIDENCE}/staging/last-success.env" ]]; then
    bad "partial_cleanup_success" "last-success written on cleanup fail"
  else
    ok "partial_cleanup_no_success"
  fi
}

scenario_lock() {
  setup_case ok
  make_dump >/dev/null
  seed_success staging
  export IRT_FAKE_FLOCK_BUSY=1
  set +e
  run_irt staging
  local rc=$?
  set -e
  unset IRT_FAKE_FLOCK_BUSY
  if [[ "$rc" -eq 60 ]]; then ok "lock_held"; else bad "lock_held" "rc=$rc"; fi
  [[ "$(read_key "${EVIDENCE}/staging/last-success.env" STATUS)" == "success" ]] \
    && ok "lock_preserves_success" \
    || bad "lock_preserves_success" "last-success changed"
}

scenario_evidence_write_fail() {
  setup_case ok
  make_dump >/dev/null
  mkdir -p "${EVIDENCE}/staging/history" "${EVIDENCE}/staging/runtime"
  # Pre-seed a valid prior success that must remain intact.
  seed_success staging
  local prior_sha prior_status
  prior_sha="$(read_key "${EVIDENCE}/staging/last-success.env" DUMP_SHA256)"
  prior_status="$(read_key "${EVIDENCE}/staging/last-success.env" STATUS)"
  # Make env evidence dir non-writable so last-attempt write fails (Unix).
  chmod 555 "${EVIDENCE}/staging" 2>/dev/null || true
  if touch "${EVIDENCE}/staging/.writetest" 2>/dev/null; then
    rm -f "${EVIDENCE}/staging/.writetest"
    chmod 755 "${EVIDENCE}/staging" 2>/dev/null || true
    skip "evidence_write" "cannot make evidence dir non-writable on this host"
    return
  fi
  set +e
  local out
  out="$(run_irt staging 2>&1)"
  local rc=$?
  set -e
  chmod 755 "${EVIDENCE}/staging" 2>/dev/null || true
  if [[ "$rc" -eq 50 ]]; then ok "evidence_write"; else bad "evidence_write" "rc=$rc"; fi
  echo "$out" | grep -q 'EVIDENCE_WRITE_FAILED\|evidence write failed' \
    && ok "evidence_write_logged" \
    || bad "evidence_write_logged" "no evidence failure log"
  # Prior success must not be clobbered / partially replaced.
  if [[ -f "${EVIDENCE}/staging/last-success.env" ]] \
    && [[ "$(read_key "${EVIDENCE}/staging/last-success.env" STATUS)" == "$prior_status" ]] \
    && [[ "$(read_key "${EVIDENCE}/staging/last-success.env" DUMP_SHA256)" == "$prior_sha" ]]; then
    ok "evidence_preserves_success"
  else
    bad "evidence_preserves_success" "last-success damaged"
  fi
  # No orphan temp publish files in the evidence dir.
  if compgen -G "${EVIDENCE}/staging/last-attempt.env.tmp."* >/dev/null 2>&1; then
    bad "evidence_no_tmp" "tmp attempt file left behind"
  else
    ok "evidence_no_tmp"
  fi
}

scenario_success_preserves_on_fail() {
  setup_case restorefail
  make_dump >/dev/null
  seed_success staging
  set +e
  run_irt staging
  local rc=$?
  set -e
  [[ "$rc" -eq 30 ]] || bad "preserve_fail_rc" "rc=$rc"
  [[ "$(read_key "${EVIDENCE}/staging/last-success.env" DUMP_BASENAME)" == "20260720T000000Z_old.dump" ]] \
    && ok "preserves_last_success" \
    || bad "preserves_last_success" "overwritten"
}

scenario_success_ok() {
  setup_case ok
  make_dump >/dev/null
  local out rc
  set +e
  out="$(run_irt staging 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then ok "success"; else bad "success" "rc=$rc"; fi
  local attempt="${EVIDENCE}/staging/last-attempt.env"
  local success="${EVIDENCE}/staging/last-success.env"
  [[ -f "$attempt" && -f "$success" ]] || { bad "success_evidence" "missing files"; return; }
  [[ "$(read_key "$success" CLEANUP_OK)" == "1" ]] || bad "success_cleanup" "CLEANUP_OK"
  [[ "$(read_key "$success" TEMP_RESOURCES_ABSENT)" == "1" ]] || bad "success_absent" "absent"
  [[ -z "$(read_key "$success" PG_RESTORE_ERROR_LOG)" ]] \
    && ok "success_no_diag" \
    || bad "success_no_diag" "PG_RESTORE_ERROR_LOG set on success"
  if [[ -f "${EVIDENCE}/staging/last-pg-restore-error.log" ]]; then
    bad "success_no_active_diag" "active error log present after success"
  else
    ok "success_no_active_diag"
  fi
  local dur
  dur="$(read_key "$success" DURATION_SEC)"
  if [[ "$dur" =~ ^[0-9]+$ ]] && (( dur >= 0 )); then
    ok "success_duration"
  else
    bad "success_duration" "dur=$dur"
  fi
  if echo "$out" | grep -Eq 'durationSec=-[0-9]'; then
    bad "success_log_duration" "$out"
  else
    ok "success_log_duration"
  fi
  # Log durationSec should match evidence when present.
  local log_dur
  log_dur="$(echo "$out" | sed -n 's/.*durationSec=\([0-9][0-9]*\).*/\1/p' | tail -n1)"
  if [[ -n "$log_dur" && "$log_dur" == "$dur" ]]; then
    ok "success_duration_match"
  elif [[ -n "$log_dur" ]]; then
    # Allow tiny skew only if both non-negative integers (should match exactly).
    bad "success_duration_match" "log=$log_dur evidence=$dur"
  else
    bad "success_duration_match" "no durationSec in log"
  fi
  if grep -Eqi 'password|token|secret|PGPASSWORD' "$success"; then
    bad "success_secrets" "secret-like content"
  else
    ok "success_no_secrets"
  fi
  if [[ -d "${STATE}/containers" ]] && compgen -G "${STATE}/containers/*" >/dev/null; then
    bad "success_docker_left" "container remains"
  else
    ok "success_docker_cleaned"
  fi
}

scenario_toctou() {
  setup_case ok
  local p
  p="$(make_dump)"
  # Race injector: rewrite dump while script copies — use a wrapper by patching PATH with a slow cp?
  # Simpler: after creating snapshot logic, change source before run by injecting via LD — hard.
  # Executable proof: start script in background is hard. Instead call snapshot path by
  # changing file between identity capture using a tiny shim around sha256sum that mutates once.
  mkdir -p "${CASE_DIR}/shim"
  cat >"${CASE_DIR}/shim/sha256sum" <<'EOF'
#!/usr/bin/env bash
# First call: hash normally. Second call (post-copy recheck): mutate source then hash.
COUNTER_FILE="${IRT_TOCTOU_COUNTER}"
SRC_FILE="${IRT_TOCTOU_SRC}"
n=0
if [[ -f "$COUNTER_FILE" ]]; then
  n="$(cat "$COUNTER_FILE")"
fi
n=$((n + 1))
echo "$n" >"$COUNTER_FILE"
if [[ "$n" -eq 2 && -n "$SRC_FILE" && -f "$SRC_FILE" ]]; then
  echo MUTATED >>"$SRC_FILE"
fi
exec /usr/bin/sha256sum "$@"
EOF
  chmod +x "${CASE_DIR}/shim/sha256sum"
  export IRT_TOCTOU_COUNTER="${CASE_DIR}/toctou.n"
  export IRT_TOCTOU_SRC="$p"
  echo 0 >"$IRT_TOCTOU_COUNTER"
  export PATH="${CASE_DIR}/shim:${BIN}:${PATH}"
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 10 ]]; then ok "toctou_detected"; else bad "toctou_detected" "rc=$rc"; fi
}

scenario_emergency() {
  setup_case ok
  make_dump >/dev/null
  mkdir -p "${EVIDENCE}/staging/runtime/deadbeefdeadbeef"
  local cid="abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
  mkdir -p "${STATE}/containers/${cid}"
  printf 'bot-tv-rt-staging-deadbeefdeadbeef\n' >"${STATE}/containers/${cid}/name"
  printf 'running\n' >"${STATE}/containers/${cid}/status"
  cat >"${STATE}/containers/${cid}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=deadbeefdeadbeef
EOF
  local cidfile="${EVIDENCE}/staging/runtime/deadbeefdeadbeef/container.cid"
  printf '%s' "$cid" >"$cidfile"
  cat >"${EVIDENCE}/staging/runtime/current.env" <<EOF
RUN_ID=deadbeefdeadbeef
CIDFILE=${cidfile}
ENVIRONMENT=staging
STARTED_AT_EPOCH=$(date +%s)
EOF
  set +e
  bash "$SCRIPT" --emergency-cleanup --environment staging --evidence-root "$EVIDENCE"
  local rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then ok "emergency_cleanup"; else bad "emergency_cleanup" "rc=$rc"; fi
  if [[ -d "${STATE}/containers/${cid}" ]]; then
    bad "emergency_absent" "container remains"
  else
    ok "emergency_absent"
  fi
}

scenario_emergency_overrides_old_attempt() {
  # N-01: old last-attempt=success must not hide a new SIGKILL run.
  setup_case ok
  seed_success staging aaaaaaaaaaaaaaaa
  local success_before
  success_before="$(cat "${EVIDENCE}/staging/last-success.env")"
  local run_b="bbbbbbbbbbbbbbbb"
  local cid="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  mkdir -p "${EVIDENCE}/staging/runtime/${run_b}" "${STATE}/containers/${cid}"
  printf 'bot-tv-rt-staging-%s\n' "$run_b" >"${STATE}/containers/${cid}/name"
  printf 'running\n' >"${STATE}/containers/${cid}/status"
  cat >"${STATE}/containers/${cid}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=${run_b}
EOF
  local cidfile="${EVIDENCE}/staging/runtime/${run_b}/container.cid"
  printf '%s' "$cid" >"$cidfile"
  local started_epoch
  started_epoch="$(( $(date +%s) - 120 ))"
  cat >"${EVIDENCE}/staging/runtime/current.env" <<EOF
RUN_ID=${run_b}
CIDFILE=${cidfile}
ENVIRONMENT=staging
STARTED_AT_EPOCH=${started_epoch}
EOF

  set +e
  out="$(bash "$SCRIPT" --emergency-cleanup --environment staging --evidence-root "$EVIDENCE" 2>&1)"
  local rc=$?
  set -e
  [[ "$rc" -eq 0 ]] && ok "n01_emergency_rc" || bad "n01_emergency_rc" "rc=$rc"
  [[ ! -d "${STATE}/containers/${cid}" ]] && ok "n01_container_removed" || bad "n01_container_removed" "still present"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" RUN_ID)" == "$run_b" ]] \
    && ok "n01_attempt_run_b" || bad "n01_attempt_run_b" "run=$(read_key "${EVIDENCE}/staging/last-attempt.env" RUN_ID)"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" STATUS)" == "failed" ]] \
    && ok "n01_attempt_failed" || bad "n01_attempt_failed" "status"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" CLEANUP_OK)" == "1" ]] \
    && ok "n01_cleanup_ok" || bad "n01_cleanup_ok" "cleanup"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" TEMP_RESOURCES_ABSENT)" == "1" ]] \
    && ok "n01_absent" || bad "n01_absent" "absent"
  local dur
  dur="$(read_key "${EVIDENCE}/staging/last-attempt.env" DURATION_SEC)"
  [[ "$dur" =~ ^[0-9]+$ ]] && (( dur >= 0 )) && ok "n01_duration_nonneg" || bad "n01_duration_nonneg" "dur=$dur"
  echo "$out" | grep -Eq 'durationSec=-[0-9]' && bad "n01_log_neg_duration" "$out" || ok "n01_log_nonneg_duration"
  [[ "$(cat "${EVIDENCE}/staging/last-success.env")" == "$success_before" ]] \
    && ok "n01_success_unchanged" || bad "n01_success_unchanged" "last-success mutated"
  if compgen -G "${EVIDENCE}/staging/history/*${run_b}*emergency_failed.env" >/dev/null; then
    ok "n01_history_b"
  else
    bad "n01_history_b" "no history for B"
  fi

  # bot-TV: no internal-health-monitor restore-test linkage yet.
  if [[ -f "${ROOT}/scripts/ops/internal-health-monitor.sh" ]]; then
    skip "n01_ihm_not_healthy" "IHM present but not wired for bot-TV restore-test"
  else
    ok "n01_ihm_absent"
  fi

  # Idempotent second emergency: marker/cid gone → no-op, last-attempt stays B.
  set +e
  bash "$SCRIPT" --emergency-cleanup --environment staging --evidence-root "$EVIDENCE" >/dev/null 2>&1
  rc=$?
  set -e
  [[ "$rc" -eq 0 ]] && ok "n01_emergency_idempotent" || bad "n01_emergency_idempotent" "rc=$rc"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" RUN_ID)" == "$run_b" ]] \
    && ok "n01_attempt_stable" || bad "n01_attempt_stable" "changed"
}

scenario_emergency_symlink_cidfile() {
  # L-01: marker CIDFILE may be a symlink; snapshot cleanup must use canonical run-dir.
  # Static assertions always run; runtime symlink semantics may SKIP on Windows/Git Bash.
  if grep -Eq 'run_dir="\$\(dirname -- "\$resolved"\)"' "$SCRIPT" \
    && ! grep -Eq 'run_dir="\$\(dirname -- "\$cidfile"\)"' "$SCRIPT"; then
    ok "l01_static_resolved_dirname"
  else
    bad "l01_static_resolved_dirname" "emergency cleanup still derives run_dir from raw cidfile"
  fi
  if grep -Eq 'basename -- "\$run_dir"\)" != "\$run_id"' "$SCRIPT" \
    && grep -Eq 'basename -- "\$resolved"\)" != "container\.cid"' "$SCRIPT"; then
    ok "l01_static_run_dir_contract"
  else
    bad "l01_static_run_dir_contract" "missing runtime/<run-id>/container.cid checks"
  fi

  setup_case ok
  local run_b="dddddddddddddddd"
  local other_run="eeeeeeeeeeeeeeee"
  local cid="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  local foreign_cid="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
  local real_run_dir link_dir link_cidfile resolved_check decoy_snapshot foreign_keep other_keep

  mkdir -p "${EVIDENCE}/staging/runtime/${run_b}" \
    "${EVIDENCE}/staging/runtime/${other_run}" \
    "${STATE}/containers/${cid}" \
    "${STATE}/containers/${foreign_cid}"

  printf 'bot-tv-rt-staging-%s\n' "$run_b" >"${STATE}/containers/${cid}/name"
  printf 'running\n' >"${STATE}/containers/${cid}/status"
  cat >"${STATE}/containers/${cid}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=${run_b}
EOF
  # Foreign owned-looking container of another RUN_ID — must survive.
  printf 'bot-tv-rt-staging-%s\n' "$other_run" >"${STATE}/containers/${foreign_cid}/name"
  printf 'running\n' >"${STATE}/containers/${foreign_cid}/status"
  cat >"${STATE}/containers/${foreign_cid}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=${other_run}
EOF

  real_run_dir="${EVIDENCE}/staging/runtime/${run_b}"
  printf '%s' "$cid" >"${real_run_dir}/container.cid"
  printf 'CANONICAL-SNAPSHOT-PAYLOAD\n' >"${real_run_dir}/dump.snapshot"
  printf 'keep-other-run\n' >"${EVIDENCE}/staging/runtime/${other_run}/keep.txt"
  other_keep="${EVIDENCE}/staging/runtime/${other_run}/keep.txt"
  foreign_keep="${EVIDENCE}/staging/runtime/foreign-keep.txt"
  printf 'foreign-root-keep\n' >"$foreign_keep"

  link_dir="${CASE_DIR}/cid-symlink-dir"
  mkdir -p "$link_dir"
  link_cidfile="${link_dir}/container.cid"
  decoy_snapshot="${link_dir}/dump.snapshot"
  printf 'DECOY-MUST-REMAIN\n' >"$decoy_snapshot"

  if ! ln -s "${real_run_dir}/container.cid" "$link_cidfile" 2>/dev/null; then
    skip "l01_symlink_cidfile" "cannot create symlink on this host — required on Linux"
    return 0
  fi
  if [[ ! -L "$link_cidfile" ]]; then
    skip "l01_symlink_cidfile" "created path is not a symlink — required on Linux"
    return 0
  fi
  if command -v realpath >/dev/null 2>&1; then
    resolved_check="$(realpath -e -- "$link_cidfile" 2>/dev/null || true)"
  else
    resolved_check="$(readlink -f -- "$link_cidfile" 2>/dev/null || true)"
  fi
  # Canonical target must be the real runtime cidfile (not the symlink dirname).
  if [[ -z "$resolved_check" || "$resolved_check" != "${real_run_dir}/container.cid" ]]; then
    # Some Windows/Git Bash builds resolve or copy differently — do not mask as PASS.
    skip "l01_symlink_cidfile" "symlink realpath semantics unreliable on this host (resolved=${resolved_check:-empty}) — required on Linux"
    return 0
  fi

  # dirname(symlink) must differ from canonical run-dir (otherwise the regression is untestable).
  [[ "$(dirname -- "$link_cidfile")" != "$real_run_dir" ]] \
    && ok "l01_symlink_dirname_differs" \
    || bad "l01_symlink_dirname_differs" "symlink dirname unexpectedly equals run-dir"

  cat >"${EVIDENCE}/staging/runtime/current.env" <<EOF
RUN_ID=${run_b}
CIDFILE=${link_cidfile}
ENVIRONMENT=staging
STARTED_AT_EPOCH=$(( $(date +%s) - 90 ))
EOF

  set +e
  bash "$SCRIPT" --emergency-cleanup --environment staging --evidence-root "$EVIDENCE" >/dev/null 2>&1
  local rc=$?
  set -e

  [[ "$rc" -eq 0 ]] && ok "l01_emergency_rc" || bad "l01_emergency_rc" "rc=$rc"
  [[ ! -e "${real_run_dir}/dump.snapshot" ]] && ok "l01_snapshot_removed" || bad "l01_snapshot_removed" "canonical snapshot remains"
  [[ -f "$decoy_snapshot" ]] && ok "l01_decoy_snapshot_kept" || bad "l01_decoy_snapshot_kept" "symlink-dir decoy deleted"
  [[ -f "$other_keep" ]] && ok "l01_other_run_kept" || bad "l01_other_run_kept" "other run-dir touched"
  [[ -f "$foreign_keep" ]] && ok "l01_foreign_file_kept" || bad "l01_foreign_file_kept" "runtime foreign file deleted"
  [[ ! -d "${STATE}/containers/${cid}" ]] && ok "l01_owned_container_removed" || bad "l01_owned_container_removed" "target remains"
  [[ -d "${STATE}/containers/${foreign_cid}" ]] && ok "l01_foreign_container_kept" || bad "l01_foreign_container_kept" "foreign RUN_ID removed"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" RUN_ID)" == "$run_b" ]] \
    && ok "l01_attempt_run_id" || bad "l01_attempt_run_id" "run=$(read_key "${EVIDENCE}/staging/last-attempt.env" RUN_ID)"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" STATUS)" == "failed" ]] \
    && ok "l01_attempt_failed" || bad "l01_attempt_failed" "status"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" SNAPSHOT_ABSENT)" == "1" ]] \
    && ok "l01_snapshot_absent" || bad "l01_snapshot_absent" "flag"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" TEMP_RESOURCES_ABSENT)" == "1" ]] \
    && ok "l01_temp_absent" || bad "l01_temp_absent" "flag"
  [[ "$(read_key "${EVIDENCE}/staging/last-attempt.env" CLEANUP_OK)" == "1" ]] \
    && ok "l01_cleanup_ok" || bad "l01_cleanup_ok" "cleanup"
  if compgen -G "${EVIDENCE}/staging/history/*${run_b}*emergency_failed.env" >/dev/null; then
    ok "l01_history_run_id"
  else
    bad "l01_history_run_id" "missing history"
  fi

  # Idempotent second pass (marker/cid gone).
  set +e
  bash "$SCRIPT" --emergency-cleanup --environment staging --evidence-root "$EVIDENCE" >/dev/null 2>&1
  rc=$?
  set -e
  [[ "$rc" -eq 0 ]] && ok "l01_idempotent" || bad "l01_idempotent" "rc=$rc"
  [[ -f "$decoy_snapshot" && -f "$other_keep" && -f "$foreign_keep" ]] \
    && ok "l01_idempotent_no_collateral" || bad "l01_idempotent_no_collateral" "collateral delete on retry"
}

scenario_emergency_same_run_finalized_noop() {
  # N-01 negative: last-attempt already finalized for same RUN_ID → no rewrite.
  setup_case ok
  local run_b="cccccccccccccccc"
  seed_success staging "$run_b"
  # Overwrite attempt as failed finalized for same run (as if main finalizer wrote it).
  cat >"${EVIDENCE}/staging/last-attempt.env" <<EOF
SCHEMA_VERSION=1
ENVIRONMENT=staging
STATUS=failed
ERROR_CODE=INTERRUPTED
STARTED_AT_UTC=2026-07-26T00:00:00Z
FINISHED_AT_UTC=2026-07-26T00:01:00Z
DURATION_SEC=60
RUN_ID=${run_b}
CLEANUP_OK=1
TEMP_RESOURCES_ABSENT=1
SNAPSHOT_ABSENT=1
USER_TABLE_COUNT=0
INTEGRITY_OK=0
EOF
  local attempt_before
  attempt_before="$(cat "${EVIDENCE}/staging/last-attempt.env")"
  local success_before
  success_before="$(cat "${EVIDENCE}/staging/last-success.env")"
  local cid="cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  mkdir -p "${EVIDENCE}/staging/runtime/${run_b}" "${STATE}/containers/${cid}"
  # Container already gone — only leftover marker/cidfile (post-cleanup finalize race).
  local cidfile="${EVIDENCE}/staging/runtime/${run_b}/container.cid"
  printf '%s' "$cid" >"$cidfile"
  # Do not create container dir — already absent.
  rmdir "${STATE}/containers/${cid}" 2>/dev/null || rm -rf "${STATE}/containers/${cid}"
  cat >"${EVIDENCE}/staging/runtime/current.env" <<EOF
RUN_ID=${run_b}
CIDFILE=${cidfile}
ENVIRONMENT=staging
STARTED_AT_EPOCH=$(date +%s)
EOF
  set +e
  bash "$SCRIPT" --emergency-cleanup --environment staging --evidence-root "$EVIDENCE" >/dev/null 2>&1
  local rc=$?
  set -e
  [[ "$rc" -eq 0 ]] && ok "n01_same_run_noop_rc" || bad "n01_same_run_noop_rc" "rc=$rc"
  [[ "$(cat "${EVIDENCE}/staging/last-attempt.env")" == "$attempt_before" ]] \
    && ok "n01_same_run_attempt_preserved" || bad "n01_same_run_attempt_preserved" "rewritten"
  [[ "$(cat "${EVIDENCE}/staging/last-success.env")" == "$success_before" ]] \
    && ok "n01_same_run_success_preserved" || bad "n01_same_run_success_preserved" "rewritten"
}

scenario_reaper() {
  setup_case ok
  # old stopped labeled
  local old="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  mkdir -p "${STATE}/containers/${old}"
  printf 'bot-tv-rt-staging-aaaaaaaaaaaaaaaa\n' >"${STATE}/containers/${old}/name"
  printf 'exited\n' >"${STATE}/containers/${old}/status"
  date -u -d '10 hours ago' +%Y-%m-%dT%H:%M:%SZ >"${STATE}/containers/${old}/created" 2>/dev/null \
    || printf '2026-01-01T00:00:00Z\n' >"${STATE}/containers/${old}/created"
  cat >"${STATE}/containers/${old}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=aaaaaaaaaaaaaaaa
EOF
  # young stopped
  local young="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  mkdir -p "${STATE}/containers/${young}"
  printf 'bot-tv-rt-staging-bbbbbbbbbbbbbbbb\n' >"${STATE}/containers/${young}/name"
  printf 'exited\n' >"${STATE}/containers/${young}/status"
  date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE}/containers/${young}/created"
  cat >"${STATE}/containers/${young}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=bbbbbbbbbbbbbbbb
EOF
  # running
  local run="cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  mkdir -p "${STATE}/containers/${run}"
  printf 'bot-tv-rt-staging-cccccccccccccccc\n' >"${STATE}/containers/${run}/name"
  printf 'running\n' >"${STATE}/containers/${run}/status"
  date -u -d '10 hours ago' +%Y-%m-%dT%H:%M:%SZ >"${STATE}/containers/${run}/created" 2>/dev/null \
    || printf '2026-01-01T00:00:00Z\n' >"${STATE}/containers/${run}/created"
  cat >"${STATE}/containers/${run}/labels" <<EOF
com.bot-tv.component=isolated-restore-test
com.bot-tv.environment=staging
com.bot-tv.run-id=cccccccccccccccc
EOF
  # wrong label
  local wrong="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  mkdir -p "${STATE}/containers/${wrong}"
  printf 'bot-tv-rt-staging-dddddddddddddddd\n' >"${STATE}/containers/${wrong}/name"
  printf 'exited\n' >"${STATE}/containers/${wrong}/status"
  date -u -d '10 hours ago' +%Y-%m-%dT%H:%M:%SZ >"${STATE}/containers/${wrong}/created" 2>/dev/null \
    || printf '2026-01-01T00:00:00Z\n' >"${STATE}/containers/${wrong}/created"
  cat >"${STATE}/containers/${wrong}/labels" <<EOF
com.bot-tv.component=other
com.bot-tv.environment=staging
com.bot-tv.run-id=dddddddddddddddd
EOF
  # working container name must never appear as delete target — create decoy without labels
  local work="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  mkdir -p "${STATE}/containers/${work}"
  printf 'tv_bot_stage-postgres-1\n' >"${STATE}/containers/${work}/name"
  printf 'exited\n' >"${STATE}/containers/${work}/status"
  date -u -d '10 hours ago' +%Y-%m-%dT%H:%M:%SZ >"${STATE}/containers/${work}/created" 2>/dev/null \
    || printf '2026-01-01T00:00:00Z\n' >"${STATE}/containers/${work}/created"

  bash "$SCRIPT" --reap-orphans --environment staging --evidence-root "$EVIDENCE"
  [[ ! -d "${STATE}/containers/${old}" ]] && ok "reaper_old_removed" || bad "reaper_old_removed" "still there"
  [[ -d "${STATE}/containers/${young}" ]] && ok "reaper_young_kept" || bad "reaper_young_kept" "removed"
  [[ -d "${STATE}/containers/${run}" ]] && ok "reaper_running_kept" || bad "reaper_running_kept" "removed"
  [[ -d "${STATE}/containers/${wrong}" ]] && ok "reaper_wrong_label_kept" || bad "reaper_wrong_label_kept" "removed"
  [[ -d "${STATE}/containers/${work}" ]] && ok "reaper_workdir_kept" || bad "reaper_workdir_kept" "removed"
}

scenario_forbidden_mutate() {
  export IRT_SKIP_FORBIDDEN_CHECK=0
  setup_case forbidden-mutate
  make_dump >/dev/null
  # Mutate mid-run via shim on docker exec first pg_isready — touch flag after inspect pre
  # Simpler: run with MODE that flips after first forbidden snapshot. Use wrapper docker.
  cat >"${BIN}/docker" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export FAKE_DOCKER_STATE="$STATE"
export FAKE_DOCKER_MODE="forbidden-mutate"
if [[ "\${1-}" == "exec" ]]; then
  touch "${STATE}/forbidden-mutated"
fi
exec bash "$FAKE_SRC" "\$@"
EOF
  chmod +x "${BIN}/docker"
  set +e
  run_irt staging
  local rc=$?
  set -e
  if [[ "$rc" -eq 80 ]]; then ok "forbidden_changed"; else bad "forbidden_changed" "rc=$rc"; fi
  export IRT_SKIP_FORBIDDEN_CHECK=1
}

scenario_offline_runner_contract() {
  skip "scenario_offline_runner_contract" "not used in bot-TV"; return 0
  : # unreachable

  setup_case ok
  local target="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" source artifact manifest archive docker_sha lock_sha archive_sha layout
  source="${CASE_DIR}/proof-source"
  artifact="${CASE_DIR}/offline-runner"
  layout="${CASE_DIR}/oci-layout"
  mkdir -p "$source" "$artifact" "$layout"
  printf 'FROM scratch\n' >"${source}/Dockerfile"
  printf '{"lockfileVersion":3}\n' >"${source}/package-lock.json"
  manifest="${artifact}/runner-${target}.manifest"
  archive="${artifact}/runner-${target}.oci.tar"
  write_oci_index() {
    local mode="$1"
    rm -rf -- "$layout"
    mkdir -p "$layout"
    case "$mode" in
      valid)
        printf '%s\n' '{"schemaVersion":2,"manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","size":1}]}' >"${layout}/index.json"
        tar -C "$layout" -cf "$archive" index.json
        ;;
      missing)
        : >"${layout}/oci-layout"
        tar -C "$layout" -cf "$archive" oci-layout
        ;;
      malformed)
        printf '%s\n' '{not-json' >"${layout}/index.json"
        tar -C "$layout" -cf "$archive" index.json
        ;;
      multiple)
        printf '%s\n' '{"schemaVersion":2,"manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","size":1},{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","size":1}]}' >"${layout}/index.json"
        tar -C "$layout" -cf "$archive" index.json
        ;;
    esac
  }
  write_oci_index valid
  docker_sha="$(sha256sum "$source/Dockerfile" | awk '{print $1}')"
  lock_sha="$(sha256sum "$source/package-lock.json" | awk '{print $1}')"
  archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
  cat >"$manifest" <<EOF
TARGET_REVISION=${target}
OCI_ARCHIVE_SHA256=${archive_sha}
DOCKERFILE_SHA256=${docker_sha}
PACKAGE_LOCK_SHA256=${lock_sha}
IMAGE_ID=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
EOF
  cat >"${BIN}/stat" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *"offline-runner"* && "\$*" == *"%U:%G:%a"* ]]; then echo root:deploy:640; else exec /usr/bin/stat "\$@"; fi
EOF
  chmod +x "${BIN}/stat"
  IRT_TARGET_REV_ARG="$target"
  IRT_PROOF_SOURCE_DIR="$source"
  IRT_OFFLINE_RUNNER_ROOT="$artifact"
  source "$OFFLINE_HELPER"
  IRT_OFFLINE_RUNNER_ROOT="$artifact"
  if irt_offline_runner_verify; then ok "offline_runner_valid"; else bad "offline_runner_valid" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  [[ "${IRT_PROOF_IMAGE:-}" == sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ]] \
    && ok "offline_runner_image_pinned" || bad "offline_runner_image_pinned" "wrong image id"
  FAKE_DOCKER_MODE=offline-oci-manifest-id
  if irt_offline_runner_verify; then ok "offline_runner_oci_manifest_id"; else bad "offline_runner_oci_manifest_id" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  [[ "${IRT_PROOF_IMAGE:-}" == sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]] \
    && ok "offline_runner_oci_image_pinned" || bad "offline_runner_oci_image_pinned" "wrong OCI image id"
  FAKE_DOCKER_MODE=offline-third-image-id
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "IMAGE_ID_MISMATCH" ]]; then ok "offline_runner_third_image_id"; else bad "offline_runner_third_image_id" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  FAKE_DOCKER_MODE=ok
  if grep -Eq '(^| )build( |$)|(^| )pull( |$)' "${STATE}/docker.log"; then bad "offline_runner_no_build_pull" "build/pull invoked"; else ok "offline_runner_no_build_pull"; fi
  rm -f -- "$manifest"
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "ARTIFACT_MISSING" ]]; then ok "offline_runner_missing"; else bad "offline_runner_missing" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  cat >"$manifest" <<EOF
TARGET_REVISION=${target}
OCI_ARCHIVE_SHA256=$(printf '0%.0s' {1..64})
DOCKERFILE_SHA256=${docker_sha}
PACKAGE_LOCK_SHA256=${lock_sha}
IMAGE_ID=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
EOF
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "ARCHIVE_SHA256_MISMATCH" ]]; then ok "offline_runner_archive_sha"; else bad "offline_runner_archive_sha" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  sed -i "s/^TARGET_REVISION=.*/TARGET_REVISION=cccccccccccccccccccccccccccccccccccccccc/; s/^OCI_ARCHIVE_SHA256=.*/OCI_ARCHIVE_SHA256=${archive_sha}/" "$manifest"
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "TARGET_REVISION_MISMATCH" ]]; then ok "offline_runner_target_revision"; else bad "offline_runner_target_revision" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  sed -i "s/^TARGET_REVISION=.*/TARGET_REVISION=${target}/; s/^DOCKERFILE_SHA256=.*/DOCKERFILE_SHA256=$(printf 'd%.0s' {1..64})/" "$manifest"
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "DOCKERFILE_SHA256_MISMATCH" ]]; then ok "offline_runner_dockerfile_sha"; else bad "offline_runner_dockerfile_sha" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  sed -i "s/^DOCKERFILE_SHA256=.*/DOCKERFILE_SHA256=${docker_sha}/; s/^PACKAGE_LOCK_SHA256=.*/PACKAGE_LOCK_SHA256=$(printf 'e%.0s' {1..64})/" "$manifest"
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "PACKAGE_LOCK_SHA256_MISMATCH" ]]; then ok "offline_runner_lock_sha"; else bad "offline_runner_lock_sha" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  sed -i "s/^PACKAGE_LOCK_SHA256=.*/PACKAGE_LOCK_SHA256=${lock_sha}/" "$manifest"
  write_oci_index missing
  archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
  sed -i "s/^OCI_ARCHIVE_SHA256=.*/OCI_ARCHIVE_SHA256=${archive_sha}/" "$manifest"
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "OCI_INDEX_INVALID" ]]; then ok "offline_runner_oci_index_missing"; else bad "offline_runner_oci_index_missing" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  write_oci_index malformed
  archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
  sed -i "s/^OCI_ARCHIVE_SHA256=.*/OCI_ARCHIVE_SHA256=${archive_sha}/" "$manifest"
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "OCI_INDEX_INVALID" ]]; then ok "offline_runner_oci_index_malformed"; else bad "offline_runner_oci_index_malformed" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  write_oci_index multiple
  archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
  sed -i "s/^OCI_ARCHIVE_SHA256=.*/OCI_ARCHIVE_SHA256=${archive_sha}/" "$manifest"
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "OCI_INDEX_INVALID" ]]; then ok "offline_runner_oci_index_multiple"; else bad "offline_runner_oci_index_multiple" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  write_oci_index valid
  archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
  sed -i "s/^OCI_ARCHIVE_SHA256=.*/OCI_ARCHIVE_SHA256=${archive_sha}/" "$manifest"
  FAKE_DOCKER_MODE=offline-label-mismatch
  if ! irt_offline_runner_verify && [[ "$IRT_OFFLINE_RUNNER_ERROR" == "OCI_LABEL_MISMATCH" ]]; then ok "offline_runner_label"; else bad "offline_runner_label" "$IRT_OFFLINE_RUNNER_ERROR"; fi
  FAKE_DOCKER_MODE=ok
  grep -Fq -- '--mount "type=bind,src=${IRT_PROOF_SOURCE_DIR}/prisma,dst=/app/prisma,readonly"' "$SCRIPT" \
    && ok "offline_runner_readonly_source_mount" || bad "offline_runner_readonly_source_mount" "mount missing"
}

scenario_proof_prisma_cli_invocation() {
  skip "scenario_proof_prisma_cli_invocation" "not used in bot-TV"; return 0
  : # unreachable

  setup_case proof-prisma-command
  local image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  if grep -Fq '"$IRT_PROOF_IMAGE" /app/node_modules/.bin/prisma migrate "${command#migrate }"' "$SCRIPT" \
    && ! grep -Fq '"$IRT_PROOF_IMAGE" migrate "${command#migrate }"' "$SCRIPT"; then
    ok "proof_prisma_local_cli_source"
  else
    bad "proof_prisma_local_cli_source" "host invocation is not explicit local Prisma CLI"
  fi
  set +e
  "$BIN/docker" run --rm \
    --network "container:abcdef0123456789" \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --mount "type=bind,src=${CASE_DIR}/prisma,dst=/app/prisma,readonly" \
    -e 'DATABASE_URL=postgresql://postgres:proof@127.0.0.1:5432/restore_test' \
    "$image" /app/node_modules/.bin/prisma migrate deploy
  local rc=$?
  set -e
  if [[ "$rc" -eq 0 ]] && grep -Fq \
    "run --rm --network container:abcdef0123456789 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m --mount type=bind,src=${CASE_DIR}/prisma,dst=/app/prisma,readonly -e DATABASE_URL=postgresql://postgres:proof@127.0.0.1:5432/restore_test ${image} /app/node_modules/.bin/prisma migrate deploy" \
    "${STATE}/docker.log"; then
    ok "proof_prisma_fake_docker_explicit_cli"
  else
    bad "proof_prisma_fake_docker_explicit_cli" "explicit Prisma CLI command not received"
  fi
}

# notready with override: patch common via env if we add it
ensure_ready_timeout_override() {
  # Inject by exporting — add to common if missing
  :
}

main() {
  echo "=== isolated-restore-test behavioral harness ==="
  scenario_dump_missing
  scenario_dump_stale
  scenario_dump_unreadable
  scenario_noimage
  scenario_notready
  scenario_restorefail
  scenario_foreign_owner_ok
  scenario_diag_cleared_on_success
  scenario_restorefail_diag_size_cap
  scenario_pg_restore_flags_contract
  scenario_integrityfail
  scenario_term
  scenario_term_during_restore
  scenario_term_after_work_ok
  scenario_child_signal_death
  scenario_rmfail
  scenario_lock
  scenario_success_preserves_on_fail
  scenario_evidence_write_fail
  scenario_success_ok
  scenario_toctou
  scenario_emergency
  scenario_emergency_purges_pg_restore_log
  scenario_emergency_overrides_old_attempt
  scenario_emergency_same_run_finalized_noop
  scenario_emergency_symlink_cidfile
  scenario_reaper
  scenario_forbidden_mutate
  # bot-TV: no offline Prisma migration-proof harness

  echo "=== summary PASS=${PASS} FAIL=${FAIL} SKIP=${SKIP} ==="
  if [[ "$FAIL" -gt 0 ]]; then
    exit 1
  fi
  exit 0
}

main "$@"
