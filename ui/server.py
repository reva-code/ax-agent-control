#!/usr/bin/env python3
"""Local backend for the AX Agent Control dashboard.

Serves index.html and a small JSON API that drives agents through AX and
Agent Substrate:

  GET  /api/state                          topology, agents, events, utilization
  POST /api/agents        {node, prompt}   start one Claude agent now on that node (review flow)
  GET  /api/tasks                          the preloaded task list per profile
  POST /api/batch         {tasks: [{prompt, role}], simulate, push}   one agent per task
  POST /api/settings      {maxRunning (0 = Substrate decides), push}
  POST /api/approve-all                    approve every agent waiting for review
  POST /api/clear                          forget finished agents
  POST /api/agents/<name>/<action>         pause | suspend | resume | dismiss | approve

Batch agents work, then wait for a human to review their diff. The scheduler
never interrupts an agent that is working; it suspends an agent to shared
snapshot storage as soon as it is waiting for review, so waiting costs no CPU
or RAM. On approval the agent is resumed wherever there is room, receives the
approval through `ax ssh`, and finishes.

Admission is Substrate's: every agent declares its CPU/memory limits, every
worker pod declares its size, and Substrate only places an agent where it fits.
When nothing fits, resume returns ResourceExhausted; the agent stays queued and
is retried once capacity frees up (an agent finishes, parks for review, or is
paused/suspended). settings.maxRunning is an optional extra cap (0 = none).

Secrets: tokens are read only inside render_task.build_task and written to a
0600 file that is deleted right after `ax apply`. Nothing sent to the browser
contains raw command output; errors are masked and cut to one short line.

  python3 ui/server.py        then open http://localhost:8787
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE.parent / "demo"
sys.path.insert(0, str(DEMO))
import format_logs  # noqa: E402
import render_task  # noqa: E402

PORT = int(os.environ.get("AX_UI_PORT", "8787"))
ATESPACE = "default"
ATE = [str(Path.home() / "go/bin/kubectl-ate"), "--context", "kind-kind"]
AX = [str(Path.home() / "go/bin/ax"), "--context", "kind-kind"]
POLL_SECONDS = 1.5
LOG_POLL_SECONDS = 3.0
METRICS_SECONDS = 4.0
SCHED_SECONDS = 2.0
MAX_PARALLEL_LAUNCHES = 4

# Steps per agent kind, and the AX-STEP marker that starts each one.
STEPS = {
    "claude-push": (["Clone repo", "Claude working", "Commit", "Push to GitHub"], ["clone", "claude", "commit", "push"]),
    "claude": (["Clone repo", "Claude working", "Diff ready", "AI review", "Waiting for review", "Finishing"],
               ["clone", "claude", "summary", "ai-review", "review", "approved"]),
    "sim": (["Plan", "Read code", "Edit", "Test", "AI review", "Waiting for review", "Finishing"],
            ["plan", "read", "edit", "test", "ai-review", "review", "approved"]),
}
# Agent profiles: the same Claude Code session in a different role, with its own
# instructions, tools, model and declared size. Every profile's diff gets a
# read-only AI review (a cheap Haiku pass) before the human review.
PROFILES = {
    "coder": {
        "label": "CODER",
        "instructions": "You are a coder agent. Implement the requested change in the Python code, keep it minimal and focused, and check it quickly with python3.",
        "tools": "Read,Edit,Write,Glob,Grep,Bash(python3:*),Bash(pytest:*)",
        "model": "", "cpu": "1", "memory": "1Gi", "ai_review": True,
    },
    "tester": {
        "label": "TESTS",
        "instructions": "You are a test-writer agent. Your job is tests: add the small function the task names only if it is missing, then write thorough unittest tests in test_app.py and run them with python3 -m unittest.",
        "tools": "Read,Edit,Write,Glob,Grep,Bash(python3:*)",
        "model": "", "cpu": "1", "memory": "1Gi", "ai_review": True,
    },
    "docs": {
        "label": "DOCS",
        "instructions": "You are a docs-writer agent. Only create or change documentation and project files (Markdown files, docstrings, .gitignore, Makefile, pyproject.toml). Never change program logic.",
        "tools": "Read,Edit,Write,Glob,Grep",
        "model": "haiku", "cpu": "500m", "memory": "768Mi", "ai_review": True,
    },
}
ROLE_TASKS = {
    "coder": [
        "Implement call_webhook in app.py as its TODO describes: retry up to 3 times with a short delay. Standard library only.",
        "Implement is_valid_ticket_id in app.py as its docstring describes.",
        'Add a summarize_ticket(ticket_id, title, tags) function to app.py that returns "[ID] Title (tags: a, b)", omitting the tags part when there are none.',
        "Add a days_since(iso_date) function to app.py that returns the number of days between a YYYY-MM-DD date and today.",
        'Add a priority_label(score) function to app.py that maps 0-3 to "low", 4-6 to "medium" and 7-10 to "high", raising ValueError otherwise.',
        "Add type hints to every function in app.py without changing behaviour.",
        "Add a slugify(title) function to app.py that lowercases, replaces runs of non-alphanumerics with '-', and strips leading/trailing dashes.",
        "Add a parse_ticket_number(ticket_id) function to app.py that returns the part after 'TICKET-' and raises ValueError for anything else.",
        "Add a normalize_tags(tags) function to app.py that lowercases, strips, de-duplicates and sorts a list of tags.",
        'Add a format_duration(seconds) function to app.py that returns strings like "1h 2m 3s", omitting zero parts.',
        "Add a truncate(text, limit) function to app.py that cuts text to at most `limit` characters, ending with an ellipsis when cut.",
        "Add an is_business_day(date) function to app.py that returns False for Saturdays and Sundays.",
        "Add a deep_merge(a, b) function to app.py that merges nested dicts, with values from b winning.",
        "Add a chunk(items, size) generator to app.py that yields lists of at most `size` items.",
        "Add a generic retry(times, delay) decorator to app.py and use it to implement call_webhook.",
        "Implement call_webhook in app.py with retries, logging every attempt and the final failure with the standard logging module.",
        "Add a small command-line entry point to app.py (argparse) that checks whether a ticket id given as an argument is valid.",
        "Add a TicketStatus Enum (OPEN, IN_PROGRESS, DONE) to app.py and a can_transition(old, new) function allowing only forward moves.",
        "Add a parse_iso_date(text) function to app.py that returns a date and raises ValueError with a clear message on bad input.",
        "Add a count_by_tag(tickets) function to app.py that takes a list of dicts with a 'tags' list and returns a tag -> count dict.",
        "Add a safe_json_loads(text, default=None) function to app.py that returns `default` instead of raising on invalid JSON.",
        "Add a mask_email(address) function to app.py that keeps the first letter of the local part and the domain, e.g. j***@example.com.",
    ],
    "tester": [
        "Implement is_valid_ticket_id in app.py, then write unittest tests for it in a new test_app.py and run them.",
        "Implement call_webhook in app.py as its TODO describes, then write unittest tests for it in test_app.py using unittest.mock and run them.",
        "Write unittest tests for a slugify(title) function in app.py (add it if missing) covering spaces, punctuation and empty input.",
        "Write unittest tests for a normalize_tags(tags) function in app.py (add it if missing) covering duplicates, case and whitespace.",
        "Write unittest tests for a truncate(text, limit) function in app.py (add it if missing) covering short text, exact length and cut text.",
        "Write unittest tests for a chunk(items, size) generator in app.py (add it if missing) covering empty lists and uneven sizes.",
        'Write unittest tests for a format_duration(seconds) function in app.py (add it if missing) covering 0, 59, 3600 and 3725 seconds.',
        "Write unittest tests for a mask_email(address) function in app.py (add it if missing) covering normal and one-letter addresses.",
        "Write unittest tests for a priority_label(score) function in app.py (add it if missing) covering each band and invalid scores.",
        "Write unittest tests for a days_since(iso_date) function in app.py (add it if missing) using a fixed date via unittest.mock.",
    ],
    "docs": [
        'Add a "Usage" section to README.md with a short example for each function in app.py.',
        "Create a CHANGELOG.md describing the functions currently in app.py, in Keep a Changelog format.",
        "Add a standard Python .gitignore file to the repository.",
        "Add a Makefile with a `test` target that runs python3 -m unittest.",
        "Add a minimal pyproject.toml for this project (name, version, requires-python >= 3.10).",
        "Add a module docstring to app.py and clear docstrings to every function that lacks one, without changing any code.",
        "Create a CONTRIBUTING.md explaining how to run the code and tests and how to propose a change.",
        "Create docs/ARCHITECTURE.md describing what app.py contains and how its functions fit together.",
        'Add a "Project status" section to README.md listing which functions are implemented and which are TODOs.',
        "Create a SECURITY.md describing how to report a vulnerability in this project.",
    ],
}
BATCH_PROMPTS = ROLE_TASKS["coder"]  # used when a batch asks only for "claude" agents
STOP = {"implement", "a", "an", "the", "to", "in", "of", "for", "and", "with", "every", "up", "add", "write", "new", "file"}
AWAKE = ("RUNNING", "RESUMING", "PAUSING", "SUSPENDING")
# Seconds without any output from an awake agent before it counts as lost. A
# simulated agent prints every few seconds; Claude can think quietly for a while.
STALL_SECONDS = {"sim": 40, "claude": 240}


class Fail(Exception):
    pass


lock = threading.RLock()
launch_slots = threading.Semaphore(MAX_PARALLEL_LAUNCHES)
agents = {}       # name -> agent dict (see new_agent)
events = []       # recent events, newest last
event_seq = 0
nodes = []        # [{"name": node, "workers": [pod, ...]}]
stats = {"completed": 0, "lastResumeMs": None, "error": None}
# push: whether batch Claude agents push their branch to GitHub once approved.
# parkDelay: seconds an agent waiting for review stays visibly in its pod before
# it is suspended to storage (a demo pace; 0 suspends it at once).
settings = {"maxRunning": 0, "push": True, "parkDelay": 15}
REPO_URL_WEB = "https://" + render_task.REPO.removesuffix(".git")
# When Substrate last said "no room", and when capacity last freed up.
capacity = {"noRoomAt": 0.0, "freedAt": 0.0}
RETRY_SECONDS = 10
usage = {
    "pods": {},            # pod -> {"cpuM": int, "memMi": int}
    "nodes": {},           # node -> {"cpuM", "memMi", "agents"}
    "totalMemMi": 0, "peakMemMi": 0, "perAgentMemMi": None, "peakAwake": 0,
}


# ---------- helpers ----------

def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def short_error(text):
    lines = [l for l in format_logs.mask(text or "").strip().splitlines() if l.strip()]
    return (lines[-1] if lines else "unknown error")[:200]


def ate_json(args):
    rc, out, err = run(ATE + args + ["-o", "json"], timeout=30)
    if rc:
        raise Fail(short_error(err))
    return json.loads(out or "{}")


def event(agent, msg, kind="log"):
    global event_seq
    with lock:
        event_seq += 1
        events.append({"id": event_seq, "t": time.time() * 1000, "agent": agent, "msg": msg, "kind": kind})
        del events[:-300]


def slug_for(prompt):
    """Name agents after the function a prompt names (call_webhook -> call-webhook), else its key words."""
    text = re.sub(r"\.[a-z]+\b", "", prompt.lower())
    ident = re.search(r"[a-z0-9]+(?:_[a-z0-9]+)+", text)
    if ident:
        slug = ident.group(0) + ("-tests" if "test" in text and "test" not in ident.group(0) else "")
    else:
        words = re.findall(r"[a-z0-9]+", text) or ["task"]
        slug = "-".join([w for w in words if w not in STOP][:3] or words[:3])
    slug = re.sub(r"-+", "-", slug.replace("_", "-"))
    if len(slug) > 30:
        slug = re.sub(r"-[^-]*$", "", slug[:30])
    return slug or "task"


def unique_name(base, taken):
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name


def new_agent(name, prompt, branch, node_hint, phase, kind="claude-push", batch=False, role=None):
    return {
        "name": name, "prompt": prompt, "branch": branch, "nodeHint": node_hint, "kind": kind, "batch": batch,
        "role": role or ("sim" if kind == "sim" else "coder"), "verdict": None,
        "phase": phase, "launchStep": "", "state": "", "worker": None, "history": [],
        "step": 0, "activity": "", "transcript": [], "done": False, "result": None, "failed": None,
        "snapshot": None, "via": None, "activeMs": 0, "stateSince": time.time(), "hold": False,
        "slices": 0, "pending": None, "sliceStart": None, "lastRan": 0.0,
        "reviewing": False, "approved": False, "reviewSince": None, "diff": [], "pushed": None,
        "restartOf": None, "_seen": set(), "_lastLogPoll": 0, "_created": "", "_lastOutput": 0.0, "_signaled": False,
    }


def public(a):
    out = {k: v for k, v in a.items() if not k.startswith("_")}
    out["steps"] = STEPS[a["kind"]][0]
    out["transcript"] = a["transcript"][-40:]
    out["stateSinceMs"] = int((time.time() - a["stateSince"]) * 1000)
    out["sliceMs"] = int((time.time() - a["sliceStart"]) * 1000) if a["sliceStart"] else None
    return out


def known_node(node):
    """The node name if the dashboard currently shows it, else None (never passed to a CLI otherwise)."""
    with lock:
        return node if node and any(n["name"] == node for n in nodes) else None


def pin_to_node(a, node):
    """Restrict the actor's next resume to workers on `node` (worker pools carry a node label)."""
    rc, _, err = run(ATE + ["update", "actor-selector", a["name"], "-a", ATESPACE, "--label", f"node={node}"])
    if rc:
        raise Fail("could not pin to node: " + short_error(err))
    a["_pinned"] = node


def unpin(a):
    """Let Substrate place the actor on any worker with room."""
    if a.get("_pinned"):
        run(ATE + ["update", "actor-selector", a["name"], "-a", ATESPACE])
        a["_pinned"] = None


def no_room(text):
    return "ResourceExhausted" in text or "no free workers" in text or "no worker has room" in text


def note_no_room(a):
    """Substrate had no worker with room for this agent: keep it waiting."""
    now = time.time()
    a["_noRoomAt"] = capacity["noRoomAt"] = now
    if not a.get("_noRoomNoted"):
        a["_noRoomNoted"] = True
        event(a["name"], "no worker has room for it right now · waiting for capacity", "shift")


def finished(a):
    return a["done"] or bool(a["failed"]) or a["phase"] in ("failed", "archived", "deleting")


# ---------- launching ----------

def build_sim_task(name, role="coder"):
    """An AX Task running demo/sim-agent.py in `role`; it carries no credentials."""
    return {
        "apiVersion": "ax.io/v1alpha1",
        "kind": "Task",
        "metadata": {"name": name, "atespace": ATESPACE},
        "spec": {
            "image": render_task.IMAGE,
            "command": ["python3", "-u", "-c", (DEMO / "sim-agent.py").read_text()],
            "env": [{"name": "TASK_NAME", "value": name}, {"name": "AGENT_ROLE", "value": role}],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
            "debug": True,  # guest services, so the approval can be delivered with `ax ssh`
        },
    }


def apply_task(a):
    """Create the AX Task for agent `a`. Claude tasks hold tokens, so their manifest
    lives in a 0600 file that is deleted straight after `ax apply`."""
    if a["kind"] == "sim":
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(build_sim_task(a["name"], a["role"] if a["role"] in PROFILES else "coder"), f)
            path = Path(f.name)
    else:
        try:
            push = a["kind"] == "claude-push" or (a["batch"] and settings["push"])
            task = render_task.build_task(a["name"], a["prompt"], a["branch"], push=push, review=a["batch"],
                                          profile=PROFILES.get(a["role"]) if a["batch"] else None)
        except SystemExit as e:  # render_task reports problems via sys.exit
            raise Fail(str(e))
        path = render_task.SECRETS / "rendered" / f"{a['name']}.yaml"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(task, f)
        del task
    try:
        rc, _, err = run(AX + ["apply", "-f", str(path)])
    finally:
        path.unlink(missing_ok=True)
    if rc:
        raise Fail("ax apply failed: " + short_error(err))


def launch(a, start=True):
    """Create the agent's Task, actor and egress policy. With start=False it is
    left suspended for the scheduler; otherwise it is started right away."""
    name = a["name"]
    with launch_slots:
        try:
            a["launchStep"] = "Creating task"
            apply_task(a)
            a["launchStep"] = "Building sandbox"
            for _ in range(120):
                if run(ATE + ["get", "actors", name, "-a", ATESPACE], timeout=20)[0] == 0:
                    break
                time.sleep(1)
            else:
                raise Fail("actor was never created")

            a["launchStep"] = "Applying egress policy"
            policy = "egress-deny.yaml" if a["kind"] == "sim" else "egress-policy.yaml"
            rc, _, err = run(ATE + ["create", "egress-policy", name, "-a", ATESPACE, "-f", str(DEMO / policy)])
            if rc:
                raise Fail("egress policy failed: " + short_error(err))
            if a["kind"] != "sim":
                event(name, "egress limited to github.com + api.anthropic.com")
        except Exception as e:
            with lock:
                a["phase"], a["failed"] = "failed", str(e)[:200]
            event(name, f"failed to start: {a['failed']}", "error")
            return
    if start:
        first_start(a, known_node(a["nodeHint"]))
    else:
        with lock:
            a["phase"], a["launchStep"] = "queued", ""
        event(name, "queued · suspended until the scheduler gives it a turn")


def first_start(a, node):
    """Start a created-but-never-run agent through AX, optionally on `node`."""
    name = a["name"]
    a["pending"], a["launchStep"] = "start", "Starting agent"
    try:
        if node:
            pin_to_node(a, node)
        # AX creates tasks suspended. Its client often drops the connection (EOF)
        # even when the resume succeeds, so the poller confirms RUNNING instead.
        rc, out, err = run(AX + ["resume", "task", name], timeout=90)
        if rc and no_room(out + err):
            with lock:
                a["phase"], a["launchStep"] = "queued", ""
            note_no_room(a)
            return
        deadline = time.time() + 120
        while time.time() < deadline:
            if a["state"] == "RUNNING":
                break
            if a["state"] == "CRASHED":
                raise Fail("sandbox crashed while starting")
            time.sleep(1)
        else:
            raise Fail("agent did not start within 2 minutes")
        with lock:
            a["phase"], a["launchStep"] = "live", ""
    except Exception as e:
        with lock:
            a["phase"], a["failed"] = "failed", str(e)[:200]
        event(name, f"failed to start: {a['failed']}", "error")
    finally:
        a["pending"] = None


# ---------- actions ----------

def act(a, action, node=None, manual=True):
    name = a["name"]
    if a["pending"]:
        return
    a["pending"] = action
    try:
        if action == "dismiss":
            archive(a, forget=True)
            return
        if manual:
            # A suspend/pause from the user takes the agent out of the scheduler's
            # rotation until the user resumes it.
            a["hold"] = action in ("pause", "suspend")
        if action == "resume":
            a["via"] = "pause" if a["state"] == "PAUSED" else "suspend"
            # A suspended agent's snapshot is in shared storage, so it may wake up on
            # any node; a paused one must return to the node holding its snapshot.
            if a["state"] == "SUSPENDED" and known_node(node):
                try:
                    pin_to_node(a, node)
                except Fail as e:
                    event(name, str(e), "error")
                    return
        if action == "resume" and not manual and not node:
            unpin(a)  # an approved agent may wake on whichever node has room
        started = time.time()
        rc, _, err = run(ATE + [action, "actor", name, "-a", ATESPACE], timeout=120)
        took = int((time.time() - started) * 1000)
        if rc and action == "resume" and no_room(err):
            note_no_room(a)
        elif rc:
            event(name, f"{action} failed: {short_error(err)}", "error")
        elif action == "resume":
            stats["lastResumeMs"] = took
    finally:
        a["pending"] = None


def archive(a, forget=False):
    """Delete a finished agent's Task and sandbox to free its slot; keep the card unless `forget`."""
    name = a["name"]
    prev = a["phase"]
    a["phase"] = "deleting"
    run(AX + ["delete", "task", name], timeout=60)
    if run(ATE + ["get", "actors", name, "-a", ATESPACE], timeout=20)[0] == 0:
        run(ATE + ["delete", "actor", name, "-a", ATESPACE], timeout=60)
    with lock:
        if forget:
            agents.pop(name, None)
            event(name, "dismissed · sandbox deleted")
        else:
            capacity["freedAt"] = time.time()
            a["phase"], a["worker"] = "archived", None
            a["state"] = "FAILED" if (a["failed"] or prev == "failed") else "DONE"
            event(name, "finished · sandbox deleted, slot freed")


# ---------- scheduler ----------

def awake(a):
    # A turn just handed out counts as awake until the agent reports RUNNING.
    turn = time.time() - a.get("_turnAt", 0) < 10 and a["state"] != "RUNNING"
    return not finished(a) and (a["state"] in AWAKE or a["pending"] in ("start", "resume") or turn)


def parked(a):
    """Suspended while it waits for review; it needs no slot until approved."""
    return a["reviewing"] and not a["approved"]


def waiting(a):
    """Needs a slot: never started, or suspended and ready to continue."""
    return (a["batch"] and not finished(a) and not a["hold"] and not a["pending"] and not awake(a)
            and not parked(a)
            and (a["phase"] == "queued" or (a["phase"] == "live" and a["state"] == "SUSPENDED")))


def least_loaded_node():
    with lock:
        load = {n["name"]: 0 for n in nodes}
        for a in agents.values():
            node = (a["worker"] or {}).get("node") or a.get("_target")
            if awake(a) and node in load:
                load[node] += 1
    return min(load, key=load.get) if load else None


def resume_turn(a):
    """Try to wake a waiting agent; Substrate decides whether and where it fits."""
    a["_turnAt"] = time.time()
    if a["phase"] == "queued":
        # A typed task keeps the node it was typed into; batch agents go anywhere.
        node = known_node(a["nodeHint"])
        threading.Thread(target=first_start, args=(a, node), daemon=True).start()
    else:
        threading.Thread(target=act, args=(a, "resume", None, False), daemon=True).start()


def replace_lost(a):
    """Mark an agent whose process vanished after a restore as failed and queue a fresh copy."""
    with lock:
        a["failed"] = "agent process lost after restore (no output); replaced"
        name = unique_name(f"{a['name']}-r", set(agents))
        branch = f"ax/{name[len('agent-'):]}-{os.urandom(2).hex()}" if a["kind"] != "sim" else None
        b = new_agent(name, a["prompt"], branch, None, "starting", kind=a["kind"], batch=True)
        b["restartOf"] = a["name"]
        agents[name] = b
    event(a["name"], f"no output for {STALL_SECONDS.get(a['kind'], 240)}s after resume · process lost · replaced by {name}", "error")
    threading.Thread(target=launch, args=(b, False), daemon=True).start()


def deliver_approval(a):
    """Write /tmp/ax-approved inside the agent's sandbox through AX's guest channel."""
    name = a["name"]
    for attempt in range(6):
        rc, _, err = run(AX + ["ssh", name, "--", "touch", "/tmp/ax-approved"], timeout=30)
        if rc == 0:
            a["_signaled"], a["_lastOutput"] = True, time.time()
            event(name, "approval delivered · continuing", "done")
            break
        time.sleep(2)
    else:
        event(name, f"could not deliver approval: {short_error(err)}", "error")
    a["pending"] = None


def approve(a):
    if not a["reviewing"] or a["approved"] or finished(a):
        return False
    a["approved"] = True
    waited = int(time.time() - (a["reviewSince"] or time.time()))
    event(a["name"], f"approved after {waited}s of review · it held no CPU or RAM while waiting")
    return True


def scheduler():
    while True:
        time.sleep(SCHED_SECONDS)
        try:
            schedule_once()
        except Exception as e:
            print("scheduler:", short_error(str(e)), file=sys.stderr)


def schedule_once():
    now = time.time()
    with lock:
        live = list(agents.values())
        # Free the slots of finished agents.
        to_archive = [a for a in live if (a["done"] or a["failed"])
                      and a["phase"] in ("live", "failed") and not a["pending"] and a["state"] != "DONE"]
        # An agent waiting for review is idle: suspend it so the wait costs nothing.
        to_park = [a for a in live if parked(a) and a["state"] == "RUNNING" and not a["pending"]
                   and now - (a["reviewSince"] or now) >= settings["parkDelay"]
                   and not finished(a)]
        # An approved agent that is awake gets its approval.
        to_signal = [a for a in live if a["approved"] and not a["_signaled"] and a["state"] == "RUNNING"
                     and not a["pending"] and not finished(a)]
        n_awake = sum(1 for a in live if awake(a))
        # Approved agents first (they are nearly done), then whoever has waited longest.
        queue = sorted((a for a in live if waiting(a)), key=lambda a: (not a["approved"], a["lastRan"]))
        # Substrate decides what fits. After a "no room" answer, wait until
        # capacity frees up (or a short timeout) before trying again.
        if capacity["freedAt"] > capacity["noRoomAt"] or now - capacity["noRoomAt"] > RETRY_SECONDS:
            ready = [a for a in queue if now - a.get("_noRoomAt", 0) > RETRY_SECONDS or capacity["freedAt"] > a.get("_noRoomAt", 0)]
        else:
            ready = []
        cap = settings["maxRunning"]
        starts = ready[:3] if not cap else ready[:max(0, min(3, cap - n_awake))]
        # Rarely, an agent's process is gone after a restore while its sandbox
        # keeps running. Detect the silence and replace the agent. Agents
        # waiting for review are quiet on purpose.
        stalled = [a for a in live if a["batch"] and not finished(a) and not a["pending"]
                   and a["state"] == "RUNNING" and a["sliceStart"] and not parked(a)
                   and (a["_signaled"] or not a["approved"])
                   and now - max(a["sliceStart"], a["_lastOutput"]) > STALL_SECONDS.get(a["kind"], 240)]
    for a in stalled:
        replace_lost(a)
    for a in to_archive:
        a["pending"] = "archive"
        threading.Thread(target=lambda a=a: (archive(a), a.update(pending=None)), daemon=True).start()
    for a in to_park:
        event(a["name"], "waiting for review · suspended to storage, holding no CPU or RAM", "shift")
        threading.Thread(target=act, args=(a, "suspend", None, False), daemon=True).start()
    for a in to_signal:
        a["pending"] = "approve"
        threading.Thread(target=deliver_approval, args=(a,), daemon=True).start()
    for a in starts:
        resume_turn(a)


# ---------- polling ----------

def poll_cluster():
    workers = ate_json(["get", "workers"]).get("workers") or []
    by_node = {}
    booked = {}
    for w in workers:
        pod = w.get("workerPod", "?")
        by_node.setdefault(w.get("nodeName", "?"), []).append(pod)
        st = w.get("status") or {}
        booked[pod] = {
            "capMemMi": round(mem_of(st.get("capacity"))), "bookedMemMi": round(mem_of(st.get("allocated"))),
            "actors": int((st.get("allocated") or {}).get("actors") or 0),
        }
    actors = ate_json(["get", "actors", "-a", ATESPACE]).get("actors") or []
    with lock:
        nodes[:] = [{"name": n, "workers": sorted(p)} for n, p in sorted(by_node.items())]
        usage["booked"] = booked
        seen = set()
        for rec in actors:
            name = rec.get("metadata", {}).get("name", "")
            status = rec.get("status", {})
            state = status.get("state", "").replace("ACTOR_STATE_", "")
            if name not in agents:
                # Agents started outside the dashboard (e.g. launch.sh) still show up.
                if not name.startswith("agent-") or state in ("CRASHED", "DELETING"):
                    continue
                agents[name] = new_agent(name, "(started outside the dashboard)", f"ax/{name}", None, "live")
            seen.add(name)
            agents[name]["_created"] = rec.get("metadata", {}).get("createTime", "")[:19]
            update_agent(agents[name], state, status)
        for name, a in list(agents.items()):
            if name not in seen and a["phase"] == "live" and not a["done"]:
                a["phase"], a["failed"] = "failed", "sandbox no longer exists"


def update_agent(a, state, status):
    name = a["name"]
    if a["phase"] in ("archived", "deleting"):
        return
    prev = a["state"]
    wa = status.get("workerAssignment") or {}
    pod, node = wa.get("workerPod"), wa.get("nodeName")
    worker = {"pod": pod, "node": node} if pod else None
    if state != prev:
        a["state"], a["stateSince"] = state, time.time()
    if state in ("RUNNING", "PAUSED", "PAUSING") and worker:
        a["worker"] = worker
    elif state in ("SUSPENDED", "SUSPENDING") and a["phase"] in ("live", "queued"):
        a["worker"] = None if state == "SUSPENDED" else a["worker"]

    local = status.get("localSnapshot") or {}
    external = status.get("externalSnapshot") or {}
    if state == "PAUSED":
        where = (local.get("nodeVmsWithLocalSnapshots") or [node or "its node"])[0]
        a["snapshot"] = {"kind": "node-local", "where": where}
    elif state == "SUSPENDED" and a["phase"] == "live":
        a["snapshot"] = {"kind": "durable", "where": "snapshot storage" if external.get("snapshotUri") else "storage"}
    elif state == "RUNNING":
        a["snapshot"] = None

    if state == "CRASHED" and a["phase"] == "live":
        a["phase"] = "failed"
        a["failed"] = format_logs.mask((status.get("crash") or {}).get("message", "sandbox crashed"))[:200]
        event(name, f"sandbox crashed: {a['failed']}", "error")

    if state == prev:
        return
    if state == "RUNNING":
        a["sliceStart"] = time.time()
        a["slices"] += 1
    elif prev == "RUNNING":
        a["lastRan"] = time.time()
        a["sliceStart"] = None
        capacity["freedAt"] = time.time()
    if not prev or a["phase"] == "deleting":
        if state == "RUNNING" and worker and not a["history"]:
            a["history"].append({**worker, "via": "start"})
            event(name, f"running on {pod} ({node})")
        return
    if state == "PAUSED":
        event(name, f"paused · snapshot kept on {node or 'its node'}")
    elif state == "SUSPENDED" and a["phase"] == "live":
        event(name, "suspended · full snapshot saved to storage · worker freed")
    elif state == "RUNNING" and worker:
        last = a["history"][-1] if a["history"] else None
        a["history"].append({**worker, "via": a["via"] or "start"})
        if not last:
            event(name, f"running on {pod} ({node})")
        elif last["node"] != node:
            event(name, f"resumed on {node} · moved from {last['node']}, same step", "shift")
        elif last["pod"] != pod:
            event(name, f"resumed on a different worker pod ({pod}) · same step", "shift")
        else:
            event(name, f"resumed on the same worker ({pod}) · same step")


def poll_logs(a):
    rc, out, _ = run(ATE + ["logs", "actors", a["name"], "-a", ATESPACE], timeout=30)
    if rc:
        return
    fresh = []
    alive = False
    with lock:
        for raw in out.splitlines():
            if raw in a["_seen"]:
                continue
            a["_seen"].add(raw)
            m = re.match(r'\{"time":"([0-9T:-]{19})', raw)
            if a["_created"] and m and m.group(1) < a["_created"]:
                continue  # from an earlier actor with the same name
            # Any new log line (e.g. Claude's thinking or API-retry events) shows the agent is alive.
            alive = True
            text = format_logs.describe_raw(raw)
            if text:
                fresh.extend(text.splitlines())
    if alive:
        a["_lastOutput"] = time.time()
    for line in fresh:
        handle_log_line(a, line)


def handle_log_line(a, line):
    name = a["name"]
    labels, markers = STEPS[a["kind"]]
    if line.startswith("▶ "):
        marker, _, rest = line[2:].partition(" ")
        if marker in markers:
            a["step"] = markers.index(marker)
            a["activity"] = labels[a["step"]]
            if a["kind"] != "sim" and marker not in ("review", "approved"):
                event(name, labels[a["step"]].lower())
        if marker == "done" and not a["done"]:
            a["done"], a["step"] = True, len(labels)
            pushed = re.match(r"([0-9a-f]{7,40}) (ax/\S+)", rest or "")
            if pushed:
                a["pushed"] = {"sha": pushed.group(1), "branch": pushed.group(2)}
                a["result"] = f"pushed {pushed.group(1)} → {pushed.group(2)}"
            else:
                a["result"] = rest or "done"
            stats["completed"] += 1
            event(name, f"done · {a['result']}", "done")
        elif marker == "verdict":
            a["verdict"] = rest
            event(name, f"AI reviewer: {rest}", "done" if rest.upper().startswith("APPROVE") else "shift")
        elif marker == "review" and not a["reviewing"]:
            a["reviewing"], a["reviewSince"] = True, time.time()
        elif marker == "approved":
            a["reviewing"] = False
        elif marker == "retry":
            a["activity"] = f"connection reset by snapshot · retrying {rest}"
            event(name, f"network connection didn't survive the snapshot · retrying {rest}", "shift")
        elif marker == "failed" and not a["failed"]:
            a["failed"] = rest or "task failed"
            event(name, f"task failed: {a['failed']}", "error")
        return
    if line.startswith("| ") and len(a["diff"]) < 120:
        a["diff"].append(line[2:])
    if line.startswith("✗"):
        # The task command exited without reporting done/failed itself.
        if not a["done"] and not a["failed"]:
            code = re.search(r"exitCode=(-?\d+)", line)
            a["failed"] = f"task command exited with code {code.group(1) if code else '?'}"
            event(name, a["failed"], "error")
        return
    a["transcript"].append(line.strip())
    a["activity"] = line.strip()


def poller():
    last = time.time()
    while True:
        try:
            poll_cluster()
            stats["error"] = None
        except Exception as e:  # keep serving; show the problem in the UI
            stats["error"] = short_error(str(e))
        now = time.time()
        with lock:
            for a in agents.values():
                if a["state"] == "RUNNING" and a["phase"] == "live" and not finished(a):
                    a["activeMs"] += int((now - last) * 1000)
            due = [a for a in agents.values()
                   if a["worker"] and a["_created"] and a["phase"] in ("live", "starting", "queued")
                   and a["state"] == "RUNNING" and not finished(a)
                   and now - a["_lastLogPoll"] >= LOG_POLL_SECONDS]
            for a in due:
                a["_lastLogPoll"] = now
        last = now
        for a in due:
            try:
                poll_logs(a)
            except Exception as e:
                print("log poll:", short_error(str(e)), file=sys.stderr)
        time.sleep(POLL_SECONDS)


def mem_of(block):
    """Memory in MiB from a Worker's capacity/allocated block ({resources: {limits: [...]}})."""
    for lim in ((block or {}).get("resources") or {}).get("limits") or []:
        if lim.get("name") == "memory":
            return to_mi(lim.get("quantity"))
    return 0


def to_mi(q):
    m = re.fullmatch(r"([0-9.]+)(Ki|Mi|Gi)?", q or "")
    if not m:
        return 0
    return float(m.group(1)) * {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, None: 1 / 1048576}[m.group(2)]


def to_millicores(q):
    m = re.fullmatch(r"([0-9.]+)(m|n)?", q or "")
    if not m:
        return 0
    return float(m.group(1)) * {"m": 1, "n": 1e-6, None: 1000}[m.group(2)]


def metrics():
    """Per-pod CPU/RAM of the worker pods (which hold the agents' sandboxes)."""
    idle = None
    while True:
        time.sleep(METRICS_SECONDS)
        try:
            rows = ate_json(["top", "workers"]).get("workers") or []
        except Exception:
            continue
        pods = {r["pod"]: {"cpuM": round(to_millicores(r.get("cpu"))), "memMi": round(to_mi(r.get("memory")))}
                for r in rows if r.get("memory") != "metrics unavailable"}
        if not pods:
            continue
        with lock:
            pod_node = {p: n["name"] for n in nodes for p in n["workers"]}
            per_node = {}
            for pod, m in pods.items():
                n = per_node.setdefault(pod_node.get(pod, "?"), {"cpuM": 0, "memMi": 0, "agents": 0})
                n["cpuM"] += m["cpuM"]
                n["memMi"] += m["memMi"]
            n_awake = n_resident = 0
            for a in agents.values():
                if a["worker"] and a["state"] in ("RUNNING", "PAUSED") and a["phase"] != "archived":
                    # Every sandbox held on a worker uses memory, finished or not.
                    n_resident += 1
                    if a["state"] == "RUNNING" and not finished(a):
                        n_awake += 1
                        if a["worker"]["node"] in per_node:
                            per_node[a["worker"]["node"]]["agents"] += 1
            total = sum(m["memMi"] for m in pods.values())
            # An empty worker pod's footprint; what is above it belongs to agents.
            floor = min(m["memMi"] for m in pods.values())
            idle = floor if idle is None else min(idle, floor)
            usage.update(pods=pods, nodes=per_node, totalMemMi=total)
            usage["peakMemMi"] = max(usage["peakMemMi"], total)
            usage["peakAwake"] = max(usage["peakAwake"], n_awake)
            if n_resident:
                sample = max(0.0, (total - idle * len(pods)) / n_resident)
                prev = usage["perAgentMemMi"]
                usage["perAgentMemMi"] = round(sample if prev is None else 0.8 * prev + 0.2 * sample, 1)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html") or self.path.startswith("/?"):
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path in ("/sessions", "/sessions.html"):
            return self.send(200, (HERE / "sessions.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/tasks":
            return self.send(200, {"roles": ROLE_TASKS, "repoUrl": REPO_URL_WEB})
        if self.path == "/api/state":
            with lock:
                body = {
                    "mode": "live", "cluster": "kind", "nodes": nodes,
                    "agents": [public(a) for a in agents.values()],
                    "events": events[-150:], "stats": dict(stats),
                    "settings": dict(settings), "usage": json.loads(json.dumps(usage)),
                    # Lets an open page notice that index.html changed and reload itself.
                    "uiVersion": int((HERE / "index.html").stat().st_mtime),
                    "repoUrl": REPO_URL_WEB,
                }
            return self.send(200, body)
        self.send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, {"error": "invalid JSON"})
        if self.path == "/api/agents":
            prompt = str(body.get("prompt", "")).strip()
            if not prompt or len(prompt) > 2000:
                return self.send(400, {"error": "prompt must be 1-2000 characters"})
            with lock:
                name = unique_name("agent-" + slug_for(prompt), set(agents))
                # A unique suffix keeps a re-run of the same task from colliding with the
                # branch an earlier run already pushed (GitHub rejects it as non-fast-forward).
                branch = f"ax/{name[len('agent-'):]}-{os.urandom(2).hex()}"
                # A typed task follows the batch flow (narrates, waits for review,
                # pushes only if enabled) but starts right away on the chosen node.
                role = body.get("role") if body.get("role") in PROFILES else "coder"
                a = new_agent(name, prompt, branch, body.get("node"), "starting", kind="claude", batch=True, role=role)
                agents[name] = a
            event(name, "task typed in · starting agent")
            threading.Thread(target=launch, args=(a,), daemon=True).start()
            return self.send(202, {"name": name})
        if self.path == "/api/batch" and isinstance(body.get("tasks"), list):
            simulate = bool(body.get("simulate"))
            if "push" in body:
                settings["push"] = bool(body["push"])
            items = []
            for t in body["tasks"][:60]:
                prompt = str((t or {}).get("prompt", "")).strip()[:2000]
                role = (t or {}).get("role") if (t or {}).get("role") in PROFILES else "coder"
                if prompt:
                    items.append((prompt, role))
            if not items:
                return self.send(400, {"error": "no tasks selected"})
            created = []
            with lock:
                taken = set(agents)
                for i, (prompt, role) in enumerate(items):
                    if simulate:
                        name = unique_name(f"sim-{role}-{i + 1:02d}", taken)
                        agents[name] = new_agent(name, f"(simulated) {prompt}", None, None, "starting",
                                                 kind="sim", batch=True, role=role)
                    else:
                        name = unique_name("agent-" + slug_for(prompt), taken)
                        # Each agent works on its own branch, which is what gets pushed.
                        branch = f"ax/{name[len('agent-'):]}-{os.urandom(2).hex()}"
                        agents[name] = new_agent(name, prompt, branch, None, "starting", kind="claude", batch=True, role=role)
                    taken.add(name)
                    created.append(agents[name])
            mode = "simulated" if simulate else ("Claude, pushing approved changes" if settings["push"] else "Claude, local only")
            event("batch", f"{len(created)} tasks queued ({mode})")
            for a in created:
                threading.Thread(target=launch, args=(a, False), daemon=True).start()
            return self.send(202, {"created": [a["name"] for a in created]})
        if self.path == "/api/batch":
            try:
                counts = {role: max(0, min(int(body.get(role, 0)), 40)) for role in PROFILES}
                counts["coder"] = max(counts["coder"], max(0, min(int(body.get("claude", 0)), 40)))
                n_sim = max(0, min(int(body.get("sim", 0)), 60))
            except (TypeError, ValueError):
                return self.send(400, {"error": "counts must be numbers"})
            simulate = bool(body.get("simulate"))
            created = []
            with lock:
                taken = set(agents)
                for role, n in counts.items():
                    tasks = ROLE_TASKS[role]
                    for i in range(n):
                        prompt = tasks[i % len(tasks)]
                        if simulate:
                            # Same role and task text, but the simulated agent: no model, no network.
                            name = unique_name(f"sim-{role}-{i + 1:02d}", taken)
                            taken.add(name)
                            agents[name] = new_agent(name, f"(simulated) {prompt}", None, None, "starting",
                                                     kind="sim", batch=True, role=role)
                        else:
                            name = unique_name("agent-" + slug_for(prompt), taken)
                            taken.add(name)
                            # Each agent works on its own branch even when it doesn't push.
                            branch = f"ax/{name[len('agent-'):]}-{os.urandom(2).hex()}"
                            agents[name] = new_agent(name, prompt, branch, None, "starting", kind="claude", batch=True, role=role)
                        created.append(agents[name])
                for i in range(n_sim):
                    name = unique_name(f"sim-{i + 1:02d}", taken)
                    taken.add(name)
                    agents[name] = new_agent(name, "Simulated agent: plan → read → edit → test (no network)",
                                             None, None, "starting", kind="sim", batch=True)
                    created.append(agents[name])
            summary = ", ".join(f"{n} {r}" for r, n in counts.items() if n) or "no profile agents"
            if simulate:
                summary += " (simulated)"
            event("batch", f"batch queued: {summary}" + (f", {n_sim} simulated" if n_sim else ""))
            for a in created:
                threading.Thread(target=launch, args=(a, False), daemon=True).start()
            return self.send(202, {"created": [a["name"] for a in created]})
        if self.path == "/api/settings":
            try:
                if "maxRunning" in body:
                    settings["maxRunning"] = max(1, min(int(body["maxRunning"]), 40))
                if "push" in body:
                    settings["push"] = bool(body["push"])
                if "parkDelay" in body:
                    settings["parkDelay"] = max(0, min(int(body["parkDelay"]), 120))
            except (TypeError, ValueError):
                return self.send(400, {"error": "settings must be numbers"})
            event("scheduler", f"push approved changes: {'on' if settings['push'] else 'off'} · wait {settings['parkDelay']}s before suspending a waiting agent")
            return self.send(200, dict(settings))
        if self.path == "/api/clear":
            with lock:
                gone = [n for n, a in agents.items() if a["phase"] == "archived"]
                for n in gone:
                    agents.pop(n, None)
            return self.send(200, {"removed": len(gone)})
        if self.path == "/api/approve-all":
            with lock:
                n = sum(approve(a) for a in list(agents.values()))
            return self.send(200, {"approved": n})
        m = re.fullmatch(r"/api/agents/([a-z0-9-]+)/approve", self.path)
        if m:
            with lock:
                a = agents.get(m.group(1))
                ok = bool(a) and approve(a)
            return self.send(200 if ok else 409, {"approved": ok})
        m = re.fullmatch(r"/api/agents/([a-z0-9-]+)/(pause|suspend|resume|dismiss)", self.path)
        if m:
            with lock:
                a = agents.get(m.group(1))
            if not a:
                return self.send(404, {"error": "no such agent"})
            threading.Thread(target=act, args=(a, m.group(2), body.get("node")), daemon=True).start()
            return self.send(202, {"ok": True})
        self.send(404, {"error": "not found"})


def main():
    for target in (poller, scheduler, metrics):
        threading.Thread(target=target, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"AX Agent Control: http://localhost:{PORT}  (mock mode: http://localhost:{PORT}/?mock)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
