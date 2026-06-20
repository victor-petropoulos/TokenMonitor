#!/usr/bin/env bash
# TokenMonitor :: stop hook → record token usage to SQLite
set -euo pipefail

INPUT="$(cat 2>/dev/null || true)"
[ -z "${INPUT}" ] && printf '{}' && exit 0

ENV_FILE="${HOME}/.cursor/token-monitor/hook.env"
if [ -f "${ENV_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

PYTHON="${TOKEN_MONITOR_PYTHON:-python3}"
SRC="${TOKEN_MONITOR_SRC:-}"

if [ -n "${SRC}" ] && [ -d "${SRC}" ]; then
  export PYTHONPATH="${SRC}${PYTHONPATH:+:${PYTHONPATH}}"
fi

printf '%s' "${INPUT}" | "${PYTHON}" -m token_monitor.hook_record 2>/dev/null || {
  TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u '+%Y-%m-%dT%H:%M:%SZ')"
  mkdir -p "${HOME}/.cursor/token-monitor"
  printf '{"ts":"%s","error":"hook_record failed"}\n' "${TS}" >> "${HOME}/.cursor/token-monitor/hook-failures.jsonl"
}

printf '{}'
exit 0
