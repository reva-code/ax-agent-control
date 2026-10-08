"""A lightweight stand-in for a coding agent, used to show density at scale.

It goes through the phases a real agent does: mostly waiting (on the model,
on tools) with short CPU bursts (running tests), gets a simulated AI review,
then waits for a human to approve its change. It has no network access and
prints the same AX-STEP progress markers as agent-run.sh, plus AX-SAY lines
narrating what it does in plain sentences. AGENT_ROLE (coder, tester, docs)
flavours what it says. Each run is seeded by TASK_NAME so it is repeatable.
"""
import hashlib
import os
import random
import time

name = os.environ.get("TASK_NAME", "sim")
role = os.environ.get("AGENT_ROLE", "coder")
rnd = random.Random(name)

ROLES = {
    "coder": {
        "jobs": ["add input checks to the ticket helpers", "make the error messages clearer",
                 "tidy up duplicated date handling", "handle an empty list without crashing"],
        "files": ["app.py", "models.py", "utils.py", "api.py"],
        "edits": ["adding a check for empty input", "renaming a variable so it reads better",
                  "moving shared code into a small helper", "making the error message more helpful"],
        "check": "Running a quick check that the new code works.",
    },
    "tester": {
        "jobs": ["write tests for the ticket helpers", "cover the date functions with tests",
                 "add tests for the edge cases nobody tested"],
        "files": ["app.py", "test_app.py", "utils.py"],
        "edits": ["adding a test for empty input", "adding a test for an invalid ticket id",
                  "adding a test for a date in the future"],
        "check": "Running the test suite.",
    },
    "docs": {
        "jobs": ["write a Usage section for the README", "add docstrings to every function",
                 "write a short CHANGELOG"],
        "files": ["README.md", "app.py", "CHANGELOG.md"],
        "edits": ["adding a usage example", "writing a clear docstring", "adding a changelog entry"],
        "check": "Re-reading the docs to check they match the code.",
    },
}
profile = ROLES.get(role, ROLES["coder"])


def step(s):
    print(f"AX-STEP: {s}", flush=True)


def say(s):
    print(f"AX-SAY: {s}", flush=True)


def wait(lo, hi):
    """Idle, as an agent does while it waits on the model or a tool."""
    time.sleep(rnd.uniform(lo, hi))


def burn(seconds):
    """Busy CPU, as when running tests."""
    end, x = time.time() + seconds, b""
    while time.time() < end:
        x = hashlib.sha256(x).digest()


step("plan")
say(f"Hi, I'm {name}, a {role} agent. My job: {rnd.choice(profile['jobs'])}.")
wait(3, 6)
say("First I'll read the code, then make a small, careful change.")
wait(1, 3)

step("read")
picked = rnd.sample(profile["files"], min(3, len(profile["files"])))
for f in picked:
    say(f"Reading {f} to understand how it works.")
    wait(2, 5)
say("I understand the code now. Time to make the change.")

step("edit")
for f in picked[:2]:
    say(f"Editing {f}: {rnd.choice(profile['edits'])}.")
    wait(3, 7)

step("test")
say(profile["check"])
burn(rnd.uniform(2, 5))
if role == "docs":
    say("The docs match the code.")
else:
    say(f"All {rnd.randint(8, 24)} tests passed.")

# A simulated AI review, standing in for the read-only Claude review pass.
step("ai-review")
say("Asking a reviewer (read-only) to check the change.")
wait(2, 4)
if rnd.random() < 0.8:
    verdict = "APPROVE: the change does what the task asks and nothing else."
else:
    verdict = "CONCERN: it also renames an unrelated variable; worth a look."
say(f"Reviewer › {verdict}")
step(f"verdict {verdict}")

# Wait for a human to approve; the scheduler suspends the agent meanwhile.
step("review")
say("My change is ready. Finishing up.")
while not os.path.exists("/tmp/ax-approved"):
    time.sleep(2)
step("approved")
say("Approved, thank you! Wrapping up.")
step(f"done approved · {len(picked[:2])} files changed (simulated)")
