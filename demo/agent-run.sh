#!/bin/sh
# Runs inside the AX sandbox as the Task command. Clones the repo, lets Claude
# Code carry out $TASK_PROMPT, then commits and pushes to $TASK_BRANCH.
#
# Lines starting with "AX-STEP:" are progress markers the dashboard reads.
# Credentials: $REPO_URL carries the GitHub token. It stays in this shell only;
# Claude gets a clean remote and an environment without it.
set -eu

step() { echo "AX-STEP: $*"; }
say() { echo "AX-SAY: $*"; }
mask() { sed -E 's#//[^/@]+@#//***@#g'; }
# Open connections don't survive a pause/suspend: after resume the agent may be
# on another worker and a transfer in flight hangs or errors. Bound every
# network git call (wall-clock timeout + stall detection) so callers can retry.
gitnet() { timeout 90 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=15 "$@"; }

export HOME=/tmp/ax-home
mkdir -p "$HOME"

step clone
say "Cloning the repository."
# The egress policy is attached just after the actor starts, so retry until
# github.com is reachable.
n=0
until gitnet clone --quiet "$REPO_URL" repo 2>/tmp/clone.err; do
  n=$((n + 1))
  if [ "$n" -ge 30 ]; then mask </tmp/clone.err; step failed clone; exit 1; fi
  rm -rf repo
  sleep 4
done
cd repo

PUSH_URL=$REPO_URL
git remote set-url origin "$(printf %s "$REPO_URL" | sed -E 's#//[^/@]+@#//#')"
# Keep build artifacts from tests out of the commit, whatever the repo's .gitignore says.
printf '__pycache__/\n*.pyc\n.pytest_cache/\n' >>.git/info/exclude
export PYTHONDONTWRITEBYTECODE=1
git config user.email "ax-agent@ax-demo.local"
git config user.name "AX agent ($TASK_NAME)"
git checkout -q -b "$TASK_BRANCH"

step claude
say "Repository ready. Handing the task to Claude."
PROMPT=$TASK_PROMPT
if [ -n "${ROLE_INSTRUCTIONS:-}" ]; then
  # The agent's profile: what kind of agent it is and what it may change.
  PROMPT="$ROLE_INSTRUCTIONS

Task: $TASK_PROMPT"
fi
MODEL_ARGS=""
if [ -n "${CLAUDE_MODEL:-}" ]; then MODEL_ARGS="--model $CLAUDE_MODEL"; fi
if [ "${REVIEW:-0}" = "1" ]; then
  # Batch agents narrate, so the Sessions page reads like a conversation.
  PROMPT="$PROMPT

As you work, start each step with one short, plain sentence saying what you are about to do."
fi
env -u REPO_URL -u AX_TASK_YAML -u AX_WORKSPACES_YAML \
  claude -p "$PROMPT" $MODEL_ARGS \
    --permission-mode acceptEdits \
    --allowedTools "${ALLOWED_TOOLS:-Read,Edit,Write,Glob,Grep,Bash(python3:*),Bash(pytest:*)}" \
    --output-format stream-json --verbose

if [ "${REVIEW:-0}" = "1" ]; then
  # Show the change, then wait for a human. The scheduler suspends the agent
  # while it waits, so waiting costs no CPU or RAM; approval arrives as a file
  # written through `ax ssh`.
  step summary
  if [ -z "$(git status --porcelain)" ]; then step failed "no changes"; exit 1; fi
  git add -A
  git diff --cached --stat | sed 's/^/AX-SAY: /'
  git diff --cached | head -40 | sed 's/^/AX-SAY: | /'
  stat=$(git diff --cached --shortstat | sed 's/^ *//')
  if [ "${AI_REVIEW:-0}" = "1" ]; then
    # A second, read-only Claude pass reviews the diff before a human does.
    step ai-review
    say "Asking a reviewer (read-only Claude) to check the change."
    verdict=$(git diff --cached | head -400 | env -u REPO_URL -u AX_TASK_YAML -u AX_WORKSPACES_YAML \
      claude -p "You are a careful code reviewer. The diff on stdin was made for this task: $TASK_PROMPT
Check that it does what the task asks and nothing risky. Reply with exactly one line:
APPROVE: <short reason>   or   CONCERN: <short reason>" \
        --model "${REVIEW_MODEL:-haiku}" --allowedTools "Read,Glob,Grep" --output-format text 2>/dev/null \
      | tr '\n' ' ' | sed 's/  */ /g; s/^ //' | cut -c1-240)
    [ -n "$verdict" ] || verdict="CONCERN: the reviewer did not answer"
    say "Reviewer › $verdict"
    step verdict "$verdict"
  fi
  step review
  say "My change is ready ($stat). Finishing up."
  until [ -f /tmp/ax-approved ]; do sleep 2; done
  step approved
  say "Approved. Finishing up."
  if [ "${PUSH:-1}" = "0" ]; then
    git commit -q -m "AX agent: $TASK_NAME" -m "Task: $TASK_PROMPT"
    step done "approved · $stat"
    exit 0
  fi
elif [ "${PUSH:-1}" = "0" ]; then
  # Batch mode without review: report what changed instead of committing and pushing.
  step summary
  if [ -z "$(git status --porcelain)" ]; then step failed "no changes"; exit 1; fi
  git add -A
  git diff --cached --stat | sed 's/^/AX-SAY: /'
  step done "$(git diff --cached --shortstat | sed 's/^ *//')"
  exit 0
fi

step commit
if [ -z "$(git status --porcelain)" ]; then step failed "no changes"; exit 1; fi
git add -A
git commit -q -F - <<EOF
AX agent: $TASK_NAME

Task: $TASK_PROMPT

Made by Claude Code running in an Agent Substrate gVisor sandbox,
orchestrated by AX.
EOF

step push
# A retry after an interrupted push is safe: if GitHub already has the commit,
# git reports it up to date and succeeds.
# GitHub connections from the cluster also fail in bursts lasting about a minute,
# so back off for roughly three minutes in total before giving up.
n=0
for wait in 3 5 10 15 20 30 40 60; do
  gitnet push -q "$PUSH_URL" "HEAD:refs/heads/$TASK_BRANCH" >/tmp/push.log 2>&1 && break
  # GitHub answered and said no (e.g. the branch already exists): retrying won't help.
  if grep -qE "\[rejected\]|\[remote rejected\]" /tmp/push.log; then mask </tmp/push.log; step failed "push rejected: branch $TASK_BRANCH already exists"; exit 1; fi
  n=$((n + 1))
  if [ "$n" -ge 8 ]; then mask </tmp/push.log; step failed push; exit 1; fi
  step retry "push $n/8"
  sleep "$wait"
done
step done "$(git rev-parse --short HEAD) $TASK_BRANCH"
