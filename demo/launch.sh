#!/bin/sh
# Apply a rendered agent Task, attach its egress policy, then delete the
# rendered file (it holds tokens).
#   ./launch.sh ~/.ax-demo-secrets/rendered/agent-webhook.yaml
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ATE="$HOME/go/bin/kubectl-ate --context kind-kind"
AX="$HOME/go/bin/ax --context kind-kind"

f=$1
name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["metadata"]["name"])' "$f")

# Output is discarded in case it echoes the spec back.
$AX apply -f "$f" >/dev/null
rm -f "$f"
echo "applied task $name (rendered manifest deleted)"

printf "waiting for actor %s" "$name"
until $ATE get actors "$name" -a default >/dev/null 2>&1; do printf .; sleep 1; done
echo

$ATE create egress-policy "$name" -a default -f "$HERE/egress-policy.yaml" >/dev/null
echo "egress policy attached: github.com, api.anthropic.com"

# AX creates tasks suspended until explicitly resumed. The ax client often
# loses its connection while waiting (EOF) even though the resume went
# through, so confirm by checking the actor state instead.
$AX resume task "$name" >/dev/null 2>&1 || true
printf "resuming"
for _ in $(seq 1 60); do
  state=$($ATE get actors -a default 2>/dev/null | awk -v n="$name" '$2==n{print $4}')
  case "$state" in
    ACTOR_STATE_RUNNING) echo; echo "agent running"; break ;;
    ACTOR_STATE_CRASHED) echo; echo "agent crashed"; exit 1 ;;
  esac
  printf .; sleep 2
done
echo "logs: $HERE/logs.sh $name"
