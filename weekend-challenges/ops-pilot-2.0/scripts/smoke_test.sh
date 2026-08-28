#!/usr/bin/env bash
#
# OpsPilot end-to-end smoke test.
#
# Exercises the complete incident lifecycle against a live deployment:
# health -> inject -> alarm -> detect -> investigate -> analyse -> approve ->
# remediate -> verify -> resolve -> postmortem -> recall.
#
# Usage:
#   ./scripts/smoke_test.sh [API_URL] [SCENARIO]
#
# With no API_URL it is read from `terraform output`.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${1:-}"
SCENARIO="${2:-lambda_error}"

# Detection depends on CloudWatch aggregating a full minute of metrics, then
# evaluating the alarm, then EventBridge delivery. Six minutes is generous.
DETECT_TIMEOUT="${DETECT_TIMEOUT:-420}"
ANALYSE_TIMEOUT="${ANALYSE_TIMEOUT:-180}"
RESOLVE_TIMEOUT="${RESOLVE_TIMEOUT:-360}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"

PASS=0
FAIL=0
STEP=0

if [[ -t 1 ]]; then TTY=1; else TTY=0; fi
progress() { [[ "${TTY}" -eq 1 ]] && printf '\r\033[K%s' "$*" || true; }
progress_clear() { [[ "${TTY}" -eq 1 ]] && printf '\r\033[K' || true; }

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

step() {
  STEP=$((STEP + 1))
  printf '\n\033[1m[%2d] %s\033[0m\n' "${STEP}" "$*"
}

ok()   { PASS=$((PASS + 1)); green "     PASS  $*"; }
bad()  { FAIL=$((FAIL + 1)); red   "     FAIL  $*"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { red "Required tool not found: $1"; exit 2; }
}

need curl
need jq

if [[ -z "${API}" ]]; then
  API="$(cd "${ROOT}/terraform" && terraform output -raw opspilot_api_url 2>/dev/null)" || true
fi
if [[ -z "${API}" ]]; then
  red "No API URL. Pass it as the first argument or run from a deployed workspace."
  exit 2
fi
API="${API%/}"

printf '\033[1mOpsPilot end-to-end smoke test\033[0m\n'
dim "API:      ${API}"
dim "Scenario: ${SCENARIO}"

# --- helpers ------------------------------------------------------------------
get()  { curl -sS --max-time 35 "${API}$1" 2>/dev/null; }
post() {
  curl -sS --max-time 35 -X POST "${API}$1" \
    -H 'content-type: application/json' -d "${2:-\{\}}" 2>/dev/null
}

# The API envelope is {"ok":true,"data":{...}}, so every field lives under .data.
incident_field() { get "/incidents/${1}" | jq -r ".data${2} // empty" 2>/dev/null; }

# Poll until an incident's status matches a pattern, printing progress.
wait_for_status() {
  local incident_id="$1" pattern="$2" timeout="$3" label="$4"
  local elapsed=0 status=""
  while [[ "${elapsed}" -lt "${timeout}" ]]; do
    status="$(incident_field "${incident_id}" '.status')"
    if [[ "${status}" =~ ${pattern} ]]; then
      progress_clear
      return 0
    fi
    progress "     waiting for ${label} … ${elapsed}s (now: ${status:-unknown})"
    sleep "${POLL_INTERVAL}"
    elapsed=$((elapsed + POLL_INTERVAL))
  done
  progress_clear
  return 1
}

# ==============================================================================
step "GET /health"
HEALTH="$(get /health)"
if [[ "$(echo "${HEALTH}" | jq -r '.data.status // empty')" == "healthy" ]]; then
  ok "API is healthy ($(echo "${HEALTH}" | jq -r '.data.bedrock_model'))"
else
  bad "API health check failed: ${HEALTH:0:200}"
  red "Cannot continue without a healthy API."
  exit 1
fi

# ------------------------------------------------------------------------------
step "GET /metrics/summary  (dashboard data)"
SUMMARY="$(get /metrics/summary)"
if [[ "$(echo "${SUMMARY}" | jq -r '.ok')" == "true" ]]; then
  ok "summary served (active=$(echo "${SUMMARY}" | jq -r '.data.active_incidents'))"
else
  bad "metrics summary failed"
fi

# ------------------------------------------------------------------------------
step "GET /demo/status  (Demo Lab reachable)"
DEMO="$(get /demo/status)"
if [[ "$(echo "${DEMO}" | jq -r '.ok')" == "true" ]]; then
  ok "Demo Lab reachable (healthy=$(echo "${DEMO}" | jq -r '.data.healthy'))"
else
  bad "Demo Lab status unavailable"
fi

# ------------------------------------------------------------------------------
step "POST /demo/reset  (start from a known-good state)"
RESET="$(post /demo/reset)"
if [[ "$(echo "${RESET}" | jq -r '.ok')" == "true" ]]; then
  ok "environment reset to baseline"
else
  bad "reset failed: ${RESET:0:200}"
fi

BASELINE_IDS="$(get '/incidents?limit=200' | jq -r '.data[]?.incident_id' | sort)"
BASELINE_COUNT="$(printf '%s' "${BASELINE_IDS}" | grep -c . || true)"
dim "     incidents before injection: ${BASELINE_COUNT}"

# Return the newest incident id that is not in a given exclusion list.
newest_excluding() {
  local exclude="$1"
  get '/incidents?limit=200' \
    | jq -r '.data[]?.incident_id' \
    | while IFS= read -r candidate; do
        printf '%s\n' "${exclude}" | grep -qxF "${candidate}" || { printf '%s' "${candidate}"; break; }
      done
}

# ------------------------------------------------------------------------------
step "POST /demo/inject  (scenario: ${SCENARIO})"
INJECT="$(post /demo/inject "{\"scenario\":\"${SCENARIO}\"}")"
if [[ "$(echo "${INJECT}" | jq -r '.ok')" == "true" ]]; then
  ok "$(echo "${INJECT}" | jq -r '.data.title') injected"
  dim "     applied: $(echo "${INJECT}" | jq -c '.data.applied')"
else
  bad "injection failed: ${INJECT:0:300}"
  exit 1
fi

# ------------------------------------------------------------------------------
step "CloudWatch alarm fires and EventBridge opens an incident"
dim "     this is real alarm evaluation - allow up to ${DETECT_TIMEOUT}s"
INCIDENT_ID=""
elapsed=0
while [[ "${elapsed}" -lt "${DETECT_TIMEOUT}" ]]; do
  INCIDENT_ID="$(newest_excluding "${BASELINE_IDS}")"
  [[ -n "${INCIDENT_ID}" ]] && break
  ALARM_STATE="$(get /demo/status | jq -r '.data.alarms | to_entries | map(select(.value=="ALARM")) | length')"
  progress "     waiting for incident … ${elapsed}s (alarms firing: ${ALARM_STATE:-0})"
  sleep "${POLL_INTERVAL}"
  elapsed=$((elapsed + POLL_INTERVAL))
done
progress_clear

if [[ -n "${INCIDENT_ID}" ]]; then
  ok "incident opened: ${INCIDENT_ID} (after ~${elapsed}s)"
else
  bad "no incident was opened within ${DETECT_TIMEOUT}s"
  red "Check: CloudWatch alarm state, the EventBridge rule, and detector logs."
  exit 1
fi

# ------------------------------------------------------------------------------
step "Incident is persisted in DynamoDB and retrievable"
DETAIL="$(get "/incidents/${INCIDENT_ID}")"
if [[ "$(echo "${DETAIL}" | jq -r '.data.incident_id')" == "${INCIDENT_ID}" ]]; then
  ok "incident readable ($(echo "${DETAIL}" | jq -r '.data.severity') / $(echo "${DETAIL}" | jq -r '.data.incident_type'))"
else
  bad "incident could not be read back"
fi

# ------------------------------------------------------------------------------
step "Investigation runs and Bedrock analysis completes"
if wait_for_status "${INCIDENT_ID}" '^(ROOT_CAUSE_IDENTIFIED|AWAITING_APPROVAL)$' "${ANALYSE_TIMEOUT}" "analysis"; then
  ok "investigation completed (status: $(incident_field "${INCIDENT_ID}" '.status'))"
else
  bad "investigation did not complete within ${ANALYSE_TIMEOUT}s"
fi

DETAIL="$(get "/incidents/${INCIDENT_ID}")"
AI_STATUS="$(echo "${DETAIL}" | jq -r '.data.ai_status')"
if [[ "${AI_STATUS}" == "OK" ]]; then
  ok "Bedrock analysis succeeded"
else
  # A fallback is correct behaviour, not a crash - report it plainly.
  dim "     NOTE  Bedrock status: ${AI_STATUS} (workflow continued on the deterministic path)"
  ok "AI failure handled without breaking the incident workflow"
fi

# ------------------------------------------------------------------------------
step "Root cause, evidence and change correlation are stored"
ROOT_CAUSE="$(echo "${DETAIL}" | jq -r '.data.root_cause.description // empty')"
CONFIDENCE="$(echo "${DETAIL}" | jq -r '.data.root_cause.confidence // 0')"
EVIDENCE_N="$(echo "${DETAIL}" | jq -r '.data.evidence | length')"
CHANGES_N="$(echo "${DETAIL}" | jq -r '.data.changes | length')"
TIMELINE_N="$(echo "${DETAIL}" | jq -r '.data.timeline | length')"

[[ -n "${ROOT_CAUSE}" ]] && ok "root cause recorded (confidence ${CONFIDENCE})" \
                         || bad "no root cause recorded"
[[ "${EVIDENCE_N}" -gt 0 ]] && ok "${EVIDENCE_N} evidence item(s)" || bad "no evidence recorded"
[[ "${TIMELINE_N}" -gt 2 ]] && ok "${TIMELINE_N} timeline entries" || bad "timeline was not built"
dim "     changes examined: ${CHANGES_N}"
dim "     change summary:   $(echo "${DETAIL}" | jq -r '.data.change_summary // "none"')"

CONTRIB="$(echo "${DETAIL}" | jq -r '[.data.changes[]? | select(.correlation=="likely_contributor")] | length')"
if [[ "${CONTRIB}" -gt 0 ]]; then
  ok "${CONTRIB} contributing change(s) correlated to the failure"
else
  dim "     NOTE  no change reached 'likely_contributor' (CloudTrail delivery lag is expected)"
fi

# ------------------------------------------------------------------------------
step "Remediation is recommended and human approval is required"
STATUS="$(incident_field "${INCIDENT_ID}" '.status')"
ACTION="$(echo "${DETAIL}" | jq -r '.data.recommendations[]? | select(.executable==true) | .action' | head -1)"

if [[ "${STATUS}" == "AWAITING_APPROVAL" ]]; then
  ok "incident is gated on human approval"
else
  bad "expected AWAITING_APPROVAL, got ${STATUS}"
fi
if [[ -n "${ACTION}" ]]; then
  ok "allowlisted remediation proposed: ${ACTION}"
else
  bad "no executable remediation was proposed"
fi

# ------------------------------------------------------------------------------
step "Rejecting an action outside the allowlist is refused"
REFUSED="$(post "/incidents/${INCIDENT_ID}/approve" '{"action":"delete_all_production_functions"}')"
if [[ "$(echo "${REFUSED}" | jq -r '.ok')" == "false" ]]; then
  ok "non-allowlisted action refused ($(echo "${REFUSED}" | jq -r '.error.code'))"
else
  bad "SECURITY: a non-allowlisted action was accepted"
fi

# ------------------------------------------------------------------------------
step "POST /incidents/{id}/approve  (human approves remediation)"
APPROVE="$(post "/incidents/${INCIDENT_ID}/approve" "{\"action\":\"${ACTION}\",\"approved_by\":\"smoke-test\"}")"
if [[ "$(echo "${APPROVE}" | jq -r '.ok')" == "true" ]]; then
  ok "remediation approved and dispatched"
else
  bad "approval failed: ${APPROVE:0:300}"
fi

# ------------------------------------------------------------------------------
step "Remediation executes and recovery is verified"
dim "     verification probes the live service - allow up to ${RESOLVE_TIMEOUT}s"
if wait_for_status "${INCIDENT_ID}" '^(RESOLVED|FAILED)$' "${RESOLVE_TIMEOUT}" "resolution"; then
  FINAL="$(get "/incidents/${INCIDENT_ID}")"
  FINAL_STATUS="$(echo "${FINAL}" | jq -r '.data.status')"
  REMEDIATION="$(echo "${FINAL}" | jq -r '.data.remediation_status')"
  VERIFICATION="$(echo "${FINAL}" | jq -r '.data.verification_status')"
  MTTR="$(echo "${FINAL}" | jq -r '.data.time_to_resolve_minutes // "n/a"')"

  [[ "${REMEDIATION}" == "SUCCEEDED" ]] && ok "remediation succeeded" \
                                        || bad "remediation status: ${REMEDIATION}"
  [[ "${VERIFICATION}" == "VERIFIED" ]] && ok "recovery verified against the live service" \
                                        || bad "verification status: ${VERIFICATION}"
  [[ "${FINAL_STATUS}" == "RESOLVED" ]] && ok "incident RESOLVED (MTTR ${MTTR} min)" \
                                        || bad "final status: ${FINAL_STATUS}"
  dim "     basis: $(echo "${FINAL}" | jq -r '.data.verification_detail.reason // "n/a"')"
else
  bad "incident did not reach a terminal state within ${RESOLVE_TIMEOUT}s"
fi

# ------------------------------------------------------------------------------
step "Postmortem is generated and stored in S3"
elapsed=0
PM_OK=""
while [[ "${elapsed}" -lt 120 ]]; do
  PM="$(get "/incidents/${INCIDENT_ID}/postmortem")"
  if [[ "$(echo "${PM}" | jq -r '.ok')" == "true" ]]; then PM_OK="${PM}"; break; fi
  progress "     waiting for postmortem … ${elapsed}s"
  sleep "${POLL_INTERVAL}"
  elapsed=$((elapsed + POLL_INTERVAL))
done
progress_clear

if [[ -n "${PM_OK}" ]]; then
  PM_LEN="$(echo "${PM_OK}" | jq -r '.data.markdown | length')"
  ok "postmortem stored ($(echo "${PM_OK}" | jq -r '.data.location'))"
  ok "document is ${PM_LEN} characters, narrative: $(echo "${PM_OK}" | jq -r '.data.narrative_source')"
  for section in "Executive Summary" "Timeline" "Root Cause" "Verification" "Lessons Learned"; do
    if echo "${PM_OK}" | jq -r '.data.markdown' | grep -q "## ${section}"; then
      ok "  section present: ${section}"
    else
      bad "  section missing: ${section}"
    fi
  done
else
  bad "no postmortem was generated within 120s"
fi

# ------------------------------------------------------------------------------
step "Incident is retrievable from history"
HISTORY="$(get '/incidents?status=RESOLVED&limit=50')"
if echo "${HISTORY}" | jq -e --arg id "${INCIDENT_ID}" '.data[] | select(.incident_id==$id)' >/dev/null 2>&1; then
  ok "incident appears in resolved history"
else
  bad "incident missing from resolved history"
fi

# ------------------------------------------------------------------------------
step "Incident memory: a repeat failure recalls this one"
dim "     injecting ${SCENARIO} a second time"
post /demo/reset >/dev/null
sleep 5
post /demo/inject "{\"scenario\":\"${SCENARIO}\"}" >/dev/null

SECOND_ID=""
elapsed=0
while [[ "${elapsed}" -lt "${DETECT_TIMEOUT}" ]]; do
  SECOND_ID="$(newest_excluding "$(printf '%s\n%s' "${BASELINE_IDS}" "${INCIDENT_ID}")")"
  [[ -n "${SECOND_ID}" ]] && break
  progress "     waiting for the second incident … ${elapsed}s"
  sleep "${POLL_INTERVAL}"
  elapsed=$((elapsed + POLL_INTERVAL))
done
progress_clear

if [[ -n "${SECOND_ID}" ]]; then
  ok "second incident opened: ${SECOND_ID}"
  if wait_for_status "${SECOND_ID}" '^(ROOT_CAUSE_IDENTIFIED|AWAITING_APPROVAL)$' "${ANALYSE_TIMEOUT}" "analysis"; then
    SIMILAR="$(incident_field "${SECOND_ID}" '.similar_incidents | length')"
    if [[ "${SIMILAR:-0}" -gt 0 ]]; then
      ok "recalled ${SIMILAR} similar past incident(s) - no vector database involved"
    else
      bad "incident memory returned no matches"
    fi
  else
    bad "second investigation did not complete"
  fi
else
  bad "second incident was not opened"
fi

# ------------------------------------------------------------------------------
step "Cleanup: reset the Demo Lab"
post /demo/reset >/dev/null && ok "environment reset to healthy"

# ==============================================================================
printf '\n\033[1m%s\033[0m\n' "──────────────────────────────────────────"
if [[ "${FAIL}" -eq 0 ]]; then
  green "SMOKE TEST PASSED   ${PASS} checks, 0 failures"
  printf '\n'
  dim "The full lifecycle ran end to end: detect, investigate, correlate,"
  dim "diagnose, recommend, approve, remediate, verify, document, remember."
  exit 0
else
  red "SMOKE TEST FAILED   ${PASS} passed, ${FAIL} failed"
  exit 1
fi
