#!/usr/bin/env bash
# Delete old "state-" Actions caches. The next run restores the newest state-* key
# (restore-keys: state-), so we keep the newest $KEEP plus this run's own key.
set -euo pipefail
REPO="${GITHUB_REPOSITORY:-syandag112-afk/tfl-collector}"
KEEP="${KEEP:-5}"; DRY_RUN="${DRY_RUN:-1}"; MINE="state-${GITHUB_RUN_ID:-none}"
gh api "repos/$REPO/actions/cache/usage" --jq '"before: \(.active_caches_count) caches, \(.active_caches_size_in_bytes/1e9*100|floor/100) GB"'
gh api --paginate "repos/$REPO/actions/caches?key=state-&sort=created_at&direction=desc&per_page=100" \
  --jq '.actions_caches[] | "\(.id) \(.key)"' > /tmp/caches.txt
total=$(wc -l < /tmp/caches.txt); n=0; del=0
while read -r id key; do
  n=$((n+1))
  if [ "$n" -le "$KEEP" ] || [ "$key" = "$MINE" ]; then continue; fi
  if [ "$DRY_RUN" = 1 ]; then echo "would delete $key"; else
    gh api -X DELETE "repos/$REPO/actions/caches/$id" >/dev/null && del=$((del+1)) || echo "failed $key"; fi
done < /tmp/caches.txt
echo "state caches: $total, kept newest $KEEP, deleted $del (dry_run=$DRY_RUN)"
