# Findings

Problems hit while running AX + Agent Substrate locally, how each was diagnosed,
and what fixed it. Environment: Apple M4 Mac, Colima (vz) VM, `kind` cluster,
gVisor nightly `release-20260824.0-120`.

## 1. Every resume crashed with `SIGILL` (arm64 pointer authentication)

**Symptom.** After any pause or suspend, the agent's processes died on resume with
`signal: illegal instruction`; agents started from a golden snapshot were fine.

**Diagnosis.**
- Reproduced with a token-free counter task, then with plain `runsc checkpoint/restore`
  outside Substrate: Alpine (musl) restored fine, Debian (glibc 2.41) crashed.
- gVisor debug logs gave the fault address; mapping it through `/proc/<pid>/maps` put it
  at libc offset `0x82644`, the same in `sh` and `sleep`.
- The instruction there is `AUTIASP`, glibc verifying a pointer-authenticated return
  address. After restore the host processes running the sandbox are new and get new
  PAC keys, so every return address signed before the checkpoint fails; the M4
  enforces this strictly and raises SIGILL.

**Fix.** Boot the VM kernel with `arm64.nopauth`. Ruled out on the way: AX snapshot
scope, async restore (`-background`), gVisor platform (systrap/ptrace), sparse copies
of checkpoint files (byte-identical), host overlayfs. x86 hosts are not affected.

## 2. AX discarded process memory on suspend

AX builds templates with `SNAPSHOT_CONTENT_SCOPE_DATA` and `OnResume.FromData = GOLDEN`:
pause/suspend kept only the workspace volume and resumed from the golden snapshot.
Added `AX_SNAPSHOT_SCOPE=full` so agents resume with their full memory.

## 3. Snapshots uploaded to a bucket that did not exist

`ax-server` defaulted `AX_SNAPSHOTS_BUCKET` to a developer's GCS bucket, so every golden
snapshot upload to the local rustfs store failed with `NoSuchBucket` and new tasks
stayed suspended forever. Pointed it at the local `ate-snapshots` bucket.

## 4. ax-server segfaulted randomly

The image was built for amd64 and ran under emulation on the arm64 node (`SIGSEGV`
inside Go's `net/http`). Rebuilt for arm64 and pinned `defaultPlatforms` in `.ko.yaml`.

## 5. Tasks never started

AX creates tasks suspended until explicitly resumed; the launcher now resumes them and
confirms the actor reached RUNNING (the `ax` client often drops the connection with
`EOF` even though the resume succeeds).

## 6. Resumes kept landing on the same node

Substrate prefers a worker still holding a stale claim for the actor, so free placement
rarely moved anything. Added `kubectl ate update actor-selector` to set the actor's
worker selector, which the scheduler applies on the next resume, and one worker pool per
node labelled `node=<name>`.

## 7. Network connections do not survive a snapshot

A suspend during `git push` left the push hanging forever. Every network git call now
has a timeout and stall detection, pushes retry with backoff, and a rejected push fails
fast. Claude Code retries its own API calls; `API_TIMEOUT_MS` is lowered so it does so
sooner.

## 8. Resource starvation looked like application bugs

With 2 CPU / 4 GB shared by two clusters, booting the Claude image starved the
Kubernetes API server, and the control plane restarted mid-snapshot. Raised the VM to
6 CPU / 10 GB. Multi-node `kind` also needed `fs.inotify.max_user_instances=512`.

## 9. Rare process loss after restore (open)

About 1 in 20–40 resumes, the agent process is gone after restore while the AX runner
survives and is never notified (`runsc ps` shows only the runner). Not reproducible with
plain gVisor. The scheduler detects the silence and replaces the agent.

## 10. Time-slicing busy agents is the wrong use of suspend

Suspending agents mid-work saves memory but cuts live model calls, costing time and
tokens. The useful pattern is suspending agents while they *wait* (here, for human
review), which saves hosting cost without interrupting any work.

## 11. Concurrency was hand-tuned because nothing declared sizes

Substrate's scheduler can admit actors by resource size, but AX templates declared no
limits (every agent counted as size zero), worker pods declared no size (each advertised
the whole node, three times over), and AX marked a task Failed when a resume hit
`ResourceExhausted`. The dashboard needed a hand-set "max working" cap. Fixed by passing
Task limits into templates, sizing worker pods, and keeping a task Suspended
(`WaitingForCapacity`) on `ResourceExhausted`; the dashboard now only queues and retries.
With 30 agents, Substrate admitted exactly 24 (6144/6144 MiB booked) and the rest waited.

## 12. Pausing frees capacity; the snapshot stays on the node's disk

Measured: while an agent works, its pod books its declared size (e.g. 256Mi/1Gi); the
moment it is paused, no worker has anything booked for it. A paused agent holds only
disk space on that node and must resume there; suspending moves the snapshot to shared
storage and frees the node's disk as well. Snapshots hold only memory actually in use
(a paused test agent's pages were ~3.4 MB on disk).
