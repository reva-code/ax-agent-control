# AX Agent Control

**Run many AI coding agents on little hardware, safely.** You pick a list of tasks;
each becomes an AI agent (Claude Code) working in its own gVisor sandbox. Agents
show their diff, get an AI review, and wait for your approval *frozen in storage*,
holding no CPU or RAM. Approve, and the agent wakes in under a second, possibly on
another machine, with its whole session intact, then commits and pushes its branch.

Built on [Google AX](https://github.com/google/ax) (agent-task orchestration) and
[Agent Substrate](https://github.com/agent-substrate/substrate) (sandboxed actor
runtime with suspend/resume), with real [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
agents doing the work.

---

## What it shows

- **Choose tasks, get one agent per task.** Pick from a preloaded list (ticked boxes,
  grouped by role) and/or type your own, one per line.
- **Agent profiles.** The same Claude Code session in different roles, each with its own
  instructions, allowed tools, model and declared size:

  | Profile | May do | Model | Size |
  |---|---|---|---|
  | Coder | edit code, run quick checks | default | 1 GiB / 1 CPU |
  | Test writer | add tests (and the function under test if missing), run them | default | 1 GiB / 1 CPU |
  | Docs writer | Markdown, docstrings and project files only | Haiku | 768 MiB / 0.5 CPU |

- **AI review before human review.** When an agent finishes, a second, read-only Claude
  pass reviews its diff and posts `APPROVE: …` or `CONCERN: …`.
- **Waiting is free.** An agent waiting for review is *suspended*: a full snapshot
  (memory, processes, files) goes to shared storage and the agent holds no CPU or RAM.
  Approving resumes it in ~0.3 s on whichever node has room.
- **Admission by real capacity.** Every agent declares its size and every worker pod
  declares its own; Agent Substrate only places an agent where it fits. When nothing
  fits, the agent waits (AX keeps it suspended rather than failing it) and starts as soon
  as capacity frees up. No hand-tuned concurrency limit.
- **Isolation.** One gVisor sandbox per agent; default-deny egress (Claude agents reach
  only `github.com` and `api.anthropic.com`; simulated agents reach nothing); agents write
  only to their own branch.
- **Pause / Suspend / Resume on any node** from the dashboard, with the move shown live.
- **Results.** A table of every finished agent: outcome, AI verdict, the nodes it ran on,
  its diff, and links to its pushed branch / pull request.
- **Simulate mode.** The same flow with lightweight stand-in agents (no model, no network)
  for free rehearsals; sandboxes, snapshots, scheduling and memory are all real.

## Architecture

```
Browser ── dashboard (ui/index.html) + Sessions page (ui/sessions.html)
   │  GET /api/state every second
ui/server.py ── launches tasks, queues, delivers approvals, reads states/logs/metrics
   │  ax CLI                         │  kubectl ate CLI                │  metrics-server
   ▼                                 ▼                                 ▼
Google AX (ax-server) ──────► Agent Substrate (ate-api-server, atelet per node,
                               egress gateway, rustfs snapshot storage)
                                     │  places actors where they fit
                                     ▼
          kind-worker (3 worker pods)        kind-worker2 (3 worker pods)
             └─ gVisor sandbox per agent        └─ gVisor sandbox per agent
                  └─ demo/agent-run.sh → claude -p "<role + task>"   (or demo/sim-agent.py)
```

**Life of an agent**

1. `server.py` creates an AX Task (image, script, role, task) with declared limits and a
   default-deny egress policy. AX turns it into a Substrate actor (suspended).
2. The dashboard tries to start it; Substrate places it on a worker pod with room, or
   answers `ResourceExhausted` and the agent stays queued.
3. Inside the sandbox: clone the repo, Claude works and narrates each step, the diff is
   printed, and a read-only Claude pass reviews it.
4. The agent waits for approval; after a short, configurable demo pause it is suspended
   to shared storage, freeing its capacity.
5. **Approve:** it is resumed wherever there is room, the approval is delivered through
   AX's guest channel (`ax ssh <task> -- touch /tmp/ax-approved`), and it commits and
   pushes its branch. Its sandbox is deleted and the slot frees.

| | Pause | Suspend |
|---|---|---|
| Frees CPU, RAM and the pod slot | yes | yes |
| Snapshot kept | on that node's disk | in shared storage |
| Can resume on | the same node | any node |

**How the dashboard stays live:** `server.py` polls Substrate for actor/worker state
(every 1.5 s), reads each awake agent's log for progress markers, Claude's narration,
the diff and the AI verdict (every 3 s), and reads real per-pod CPU/memory from
metrics-server (every 4 s). The page polls the server every second.

## Measured on a laptop (2 worker nodes, 6 worker pods × 1 GiB, 6 CPU / 10 GB VM)

| | |
|---|---|
| Resume call | ~0.2–0.45 s |
| Approval delivered after resume | ~0.7 s |
| Empty warm worker pod | ~8–30 MB RAM, ~0 CPU |
| Simulated agent while working | ~100–170 MB |
| Agent waiting for review | 0 MB RAM, 0 CPU (snapshot on disk) |
| 25 agents, max 6 working | peak 0.72 GB vs ~3 GB if all ran at once (~4×) |
| 30 simulated agents, Substrate admission | 24 admitted (6144/6144 MiB booked), 6 waited, all parked for review by 90 s, Approve all → done in 30 s, 0 failures |
| Pause | pod booking drops to 0 immediately |

Model costs are unchanged by any of this; what goes down is the cost of *hosting*
agents while they wait (memory and reserved capacity).

## Repository layout

```
ui/
  server.py        backend: tasks, queue, admission retries, approvals, logs, metrics
  index.html       dashboard: task picker, nodes/pods/agents, snapshot storage, results
  sessions.html    one live terminal per agent
demo/
  agent-run.sh     in the sandbox: clone → Claude (role) → diff → AI review → wait → commit/push
  sim-agent.py     simulated agent per role, same steps, no model, no network
  render_task.py   builds an AX Task with a profile; reads tokens from ~/.ax-demo-secrets
  format_logs.py   turns actor logs into a readable transcript, masking token-shaped text
  launch.sh, logs.sh                     command-line path for a single agent
  egress-policy.yaml, egress-deny.yaml   network allow-lists
  workerpools.yaml one Substrate worker pool per node, sized 1Gi / 2 CPU per pod
patches/
  ax.patch         changes to google/ax @ ac23328
  substrate.patch  changes to agent-substrate/substrate @ 7317e08
docs/
  FINDINGS.md      problems found and fixed along the way
```

## Changes to the upstream projects

**AX** (`patches/ax.patch`)
- Task `resources.limits` are passed into the Substrate ActorTemplate, so Substrate books
  them against worker capacity and sizes the sandbox to them.
- A resume rejected with `ResourceExhausted` keeps the task **Suspended**
  (`WaitingForCapacity`) and returns `ResourceExhausted`, instead of marking it Failed.
  Covered by new unit tests.
- `AX_SNAPSHOT_SCOPE=full`: pause/suspend keep process memory (AX's default `DATA` scope
  discards it and resumes from the golden snapshot).
- Deploy config for a local rustfs bucket; `ko` builds for `linux/arm64`.
- `Dockerfile.task-runner-claude`: task-runner image with git and the Claude Code CLI.

**Substrate** (`patches/substrate.patch`)
- `kubectl ate update actor-selector <actor> -a <atespace> --label node=…` sets an actor's
  worker selector, so its next resume lands on the chosen node. With unit tests.
- `hack/create-kind-cluster.sh`: `KIND_WORKERS=N` adds worker nodes.

## Setup

> Tested on an Apple-Silicon Mac with Colima. On x86 Linux skip the `arm64.nopauth` step
> (see [docs/FINDINGS.md](docs/FINDINGS.md)).

**Prerequisites:** Docker (Colima with ≥ 6 CPU / 10 GB), `kind`, `kubectl`, Go, `ko`,
Python 3.10+, a GitHub repo for the agents to work on, a GitHub token and an Anthropic
API key.

1. **Upstream projects at the tested commits, with the patches**
   ```sh
   git clone https://github.com/google/ax && git -C ax checkout ac23328
   git clone https://github.com/agent-substrate/substrate && git -C substrate checkout 7317e08
   (cd ax && git apply ../ax-agent-control/patches/ax.patch)
   (cd substrate && git apply ../ax-agent-control/patches/substrate.patch)
   ```
2. **Apple Silicon only** (pointer authentication off; inotify limit for multi-node kind)
   ```sh
   echo 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT arm64.nopauth"' | colima ssh -- sudo tee /etc/default/grub.d/99-nopauth.cfg
   echo 'fs.inotify.max_user_instances = 512' | colima ssh -- sudo tee /etc/sysctl.d/99-kind.conf
   colima ssh -- sudo update-grub && colima restart
   ```
3. **Cluster, Substrate, worker pools, metrics**
   ```sh
   cd substrate
   KIND_WORKERS=2 hack/create-kind-cluster.sh
   hack/install-ate-kind.sh --deploy-ate-system
   go build -o ~/go/bin/kubectl-ate ./cmd/kubectl-ate
   KO_DOCKER_REPO=localhost:5001 KO_DEFAULTPLATFORMS=linux/arm64 \
     ko resolve -f ../ax-agent-control/demo/workerpools.yaml | kubectl --context kind-kind apply -f -
   kubectl --context kind-kind apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
   kubectl --context kind-kind -n kube-system patch deployment metrics-server --type json \
     -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
   ```
4. **AX and the agent image**
   ```sh
   cd ../ax
   KO_DOCKER_REPO=localhost:5001 ko resolve -f deploy/ | kubectl --context kind-kind apply -f -
   go install ./cmd/ax
   GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o bin/linux_arm64/ax-task-runner ./cmd/ax-task-runner
   docker build -f Dockerfile.task-runner-claude -t localhost:5001/ax-task-runner-claude:latest .
   docker push localhost:5001/ax-task-runner-claude:latest
   ```
   Set `IMAGE` in `demo/render_task.py` to the pushed digest and `REPO` to the repository
   the agents should work on.
5. **Secrets** (read only by `render_task.py`; never printed or sent to the browser)
   ```sh
   mkdir -m 700 -p ~/.ax-demo-secrets
   nano ~/.ax-demo-secrets/github-token   # token with push access to REPO
   nano ~/.ax-demo-secrets/claude-token   # sk-ant-api… (or a Claude Code OAuth token)
   chmod 600 ~/.ax-demo-secrets/*
   ```
6. **Dashboard**
   ```sh
   python3 ui/server.py      # http://localhost:8787 · Sessions: /sessions · offline UI: /?mock
   ```

## Using it

1. **Choose tasks:** tick preloaded tasks and/or type your own (`tests:` / `docs:` prefixes
   pick the role). Options: *Push approved changes to GitHub*, *Simulate*, and the demo
   pace (seconds an agent keeps working visibly before it is parked).
2. **Run:** watch agents land on pods, work, and park in *Snapshot storage* as
   *Waiting for review* with their AI verdict; follow them on the Sessions page.
3. **Approve** one by one or *Approve all waiting*. Pause / Suspend / *Resume on a node*
   work on any working agent.
4. **Results** at the bottom: outcome, verdict, nodes, diff, branch and pull-request links.

## Security notes

- Tokens live in `~/.ax-demo-secrets` (mode 600); the rendered task file is written 0600
  and deleted right after `ax apply`.
- Claude never sees the GitHub token: it is removed from the git remote and from Claude's
  environment. Each profile has a restricted tool list; the AI reviewer is read-only.
- Agents write only to their own branch; a human approves before anything is pushed.
- Known gap: the Claude key is in the task environment and so is captured in snapshots.
  Substrate's egress credential injection is the production fix.

## Limitations

- Local `kind` cluster: both "nodes" are containers in one VM.
- Declared agent sizes are estimates; tune them from the Utilization panel.
- Rarely (about 1 in 20–40 resumes) an agent's process is gone after a restore; the
  scheduler detects the silence and replaces it. Root cause not yet found.
- AX and Substrate are pre-1.0 and change quickly; the patches target the commits above.

## License

Apache-2.0, matching the upstream projects. See [LICENSE](LICENSE).
