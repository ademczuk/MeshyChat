#!/usr/bin/env bash
# Two nodes, one tracked ask, answered. Run inside the server container:
#   docker compose -f deploy/quickstart/compose.yaml exec meshychat bash deploy/quickstart/demo.sh
set -euo pipefail
S=/state
BASE=http://127.0.0.1:8765
N="python -m tools.meshy_node"
jget() { python -c "import json,sys; print(json.load(sys.stdin)$1)"; }

rm -f "$S/nodeA.json" "$S/nodeB.json"
CODES=$($N pair --base-url "$BASE" --state-file "$S/operator.json" --count 2)
CODE_A=$(printf '%s' "$CODES" | jget "['codes'][0]['code']")
CODE_B=$(printf '%s' "$CODES" | jget "['codes'][1]['code']")
A_ID=$($N mint --base-url "$BASE" --state-file "$S/nodeA.json" --code "$CODE_A" --device-label node-a | jget "['peer_id']")
B_ID=$($N mint --base-url "$BASE" --state-file "$S/nodeB.json" --code "$CODE_B" --device-label node-b | jget "['peer_id']")
for ID in "$A_ID" "$B_ID"; do
  $N grant --base-url "$BASE" --state-file "$S/operator.json" --namespace shared/mesh-chat \
    --grantee "$ID" --permission write > /dev/null
done
B_LABEL="peer-${B_ID:0:8}"

echo "--- node-b starts listening"
$N listen --base-url "$BASE" --state-file "$S/nodeB.json" --read --count 1 --duration 25 > /tmp/listen.out 2>&1 &
LISTENER=$!
sleep 3
echo "--- node-a asks $B_LABEL a question that needs an answer"
KEY=$($N send --base-url "$BASE" --state-file "$S/nodeA.json" --to "$B_LABEL" --thread demo \
  --type request --expects-reply --body "Hello from node-a. Can you confirm you got this?" | jget "['key']")
echo "sent $KEY"
wait "$LISTENER" || true
echo "--- what node-b's listener printed"
cat /tmp/listen.out
echo "--- the ask appears on node-b's owed list (the server files it within a few seconds)"
for _ in $(seq 1 40); do
  N_OWED=$($N owed --base-url "$BASE" --state-file "$S/nodeB.json" | jget "['summary']")
  case "$N_OWED" in "open 1"*) break ;; esac
  sleep 2
done
echo "$N_OWED"
echo "--- node-b answers and closes it"
$N send --base-url "$BASE" --state-file "$S/nodeB.json" --to peer-${A_ID:0:8} --thread demo \
  --type answer --in-reply-to "$KEY" --closes "$KEY" --body "Got it." | jget "['status']"
echo "--- node-b's owed list after the answer (again filed within a few seconds)"
for _ in $(seq 1 40); do
  N_OWED=$($N owed --base-url "$BASE" --state-file "$S/nodeB.json" | jget "['summary']")
  case "$N_OWED" in "open 0"*) break ;; esac
  sleep 2
done
echo "$N_OWED"
