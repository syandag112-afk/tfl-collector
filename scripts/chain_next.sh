#!/usr/bin/env bash
# Chain the next collect run (closes GitHub schedule gaps).
# A queued/waiting/pending run younger than STUCK_MIN minutes is a real next run: do not dispatch a duplicate.
# One older than that is stuck (e.g. GitHub outage 19 Oct 16:49-17:57 BST, 18 Oct 02:59-03:15): cancel it and
# dispatch a fresh run anyway. Another in_progress run (not this one) that started within BUSY_MIN minutes will
# chain itself, so it also blocks a duplicate dispatch; an older one is past timeout-minutes and is ignored.
set -uo pipefail
REPO="${GITHUB_REPOSITORY:-syandag112-afk/tfl-collector}"
SELF="${GITHUB_RUN_ID:-0}"
STUCK_MIN="${STUCK_MIN:-5}"
BUSY_MIN="${BUSY_MIN:-15}"
NOW="${NOW:-$(date -u +%s)}"
REF="${REF:-main}"

runs=$(gh run list -R "$REPO" -w collect --limit 50 --json databaseId,status,createdAt,startedAt \
  --jq '.[] | select(.status != "completed") | "\(.databaseId) \(.status) \(.createdAt) \(.startedAt // .createdAt)"') || {
  echo "::warning::could not list runs; dispatching anyway (concurrency group stops a real collision)"; runs=""; }

fresh=0; busy=0; stuck=0
while read -r id status created started; do
  [ -z "${id:-}" ] && continue
  [ "$id" = "$SELF" ] && continue
  case "$status" in
    queued|waiting|pending|requested)
      age=$(( (NOW - $(date -u -d "$created" +%s)) / 60 ))
      if [ "$age" -ge "$STUCK_MIN" ]; then
        stuck=$((stuck+1))
        echo "run $id $status for ${age} min -> stuck, cancelling"
        gh run cancel "$id" -R "$REPO" >/dev/null 2>&1 \
          || gh api -X POST "repos/$REPO/actions/runs/$id/force-cancel" >/dev/null 2>&1 \
          || echo "::warning::could not cancel stuck run $id"
      else
        fresh=$((fresh+1)); echo "run $id $status for ${age} min -> next run already queued"
      fi;;
    in_progress)
      age=$(( (NOW - $(date -u -d "$started" +%s)) / 60 ))
      if [ "$age" -lt "$BUSY_MIN" ]; then busy=$((busy+1)); echo "run $id in_progress for ${age} min -> it will chain itself"
      else echo "run $id in_progress for ${age} min -> past timeout, ignored"; fi;;
  esac
done <<< "$runs"

if [ "$fresh" -gt 0 ] || [ "$busy" -gt 0 ]; then
  echo "not dispatching: fresh queued=$fresh, other in_progress=$busy (stuck cancelled=$stuck)"
  echo "DISPATCHED=0"
  exit 0
fi
if gh workflow run collect.yml -R "$REPO" --ref "$REF"; then
  echo "dispatched next run (stuck cancelled=$stuck)"; echo "DISPATCHED=1"
else
  echo "::warning::dispatch failed (the */5 schedule is the fallback)"; echo "DISPATCHED=0"
fi
exit 0
