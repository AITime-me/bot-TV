#!/usr/bin/env bash
# Isolated restore-test freshness / TTL policy (single runtime source of truth).
# Safe to source from restore-test. No side effects beyond defining readonly ints.

if [[ "${IRT_POLICY_LOADED:-0}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

# Dump selection age for a restore-test run (hours).
IRT_DUMP_MAX_AGE_HOURS=36
# Stopped orphan reaper TTL (hours).
IRT_ORPHAN_TTL_HOURS=6

irt_policy_validate() {
  local name value
  for name in IRT_DUMP_MAX_AGE_HOURS IRT_ORPHAN_TTL_HOURS; do
    value="${!name-}"
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
      echo "error: isolated-restore-test policy invalid: ${name}=${value:-empty}" >&2
      return 1
    fi
  done
  return 0
}

if ! irt_policy_validate; then
  exit 70
fi

readonly IRT_DUMP_MAX_AGE_HOURS
readonly IRT_ORPHAN_TTL_HOURS
readonly IRT_POLICY_LOADED=1
