#!/usr/bin/env python3
"""Render an AX Task that runs Claude Code against tag-transition-target.

Reads tokens from ~/.ax-demo-secrets, writes the manifest to
~/.ax-demo-secrets/rendered/<name>.yaml (mode 600) and prints only that path.
Token values are never printed, including on errors.

  python3 render_task.py agent-webhook \
      "Implement call_webhook in app.py as its TODO describes. Standard library only."
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRETS = Path.home() / ".ax-demo-secrets"
REPO = "github.com/reva-code/tag-transition-target.git"
IMAGE = ("localhost:5001/ax-task-runner-claude@sha256:"
         "38645b30a5b49a88c6c521c6858c2f607c8089502184e584b2dc9965a899415c")


def read_secret(name):
    # Tokens never contain whitespace; drop any that a paste added (including a
    # line break in the middle of the key, which .strip() would keep).
    value = "".join((SECRETS / name).read_text().split())
    if not value:
        sys.exit(f"error: {SECRETS / name} is empty")
    return value


def claude_env_name(token):
    if token.startswith("sk-ant-oat"):
        return "CLAUDE_CODE_OAUTH_TOKEN"
    if token.startswith("sk-ant-api"):
        return "ANTHROPIC_API_KEY"
    sys.exit("error: claude-token has an unrecognized format (expected sk-ant-oat... or sk-ant-api...)")


def build_task(name, prompt, branch, push=True, review=False, profile=None):
    """profile (optional): {"instructions", "tools", "model", "cpu", "memory", "ai_review"}."""
    profile = profile or {}
    github = read_secret("github-token")
    claude = read_secret("claude-token")
    env = {
        "TASK_NAME": name,
        "TASK_PROMPT": prompt,
        "TASK_BRANCH": branch,
        "PUSH": "1" if push else "0",
        "REVIEW": "1" if review else "0",
        "AI_REVIEW": "1" if profile.get("ai_review") else "0",
        "REPO_URL": f"https://x-access-token:{github}@{REPO}",
        claude_env_name(claude): claude,
        "IS_SANDBOX": "1",
        # Only api.anthropic.com is allowed out; skip telemetry/update calls.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        # A Claude API request in flight during a pause/suspend loses its
        # connection; time it out after 2 min (default 10) so Claude Code retries.
        "API_TIMEOUT_MS": "120000",
    }
    for key, var in (("instructions", "ROLE_INSTRUCTIONS"), ("tools", "ALLOWED_TOOLS"), ("model", "CLAUDE_MODEL")):
        if profile.get(key):
            env[var] = profile[key]
    return {
        "apiVersion": "ax.io/v1alpha1",
        "kind": "Task",
        "metadata": {"name": name, "atespace": "default"},
        "spec": {
            "image": IMAGE,
            "command": ["sh", "-c", (HERE / "agent-run.sh").read_text()],
            "env": [{"name": k, "value": v} for k, v in env.items()],
            # AX passes the limits to Substrate, which books them against a
            # worker's capacity and sizes the sandbox to them.
            "resources": {
                "requests": {"cpu": "250m", "memory": profile.get("memory", "1Gi")},
                "limits": {"cpu": profile.get("cpu", "1"), "memory": profile.get("memory", "1Gi")},
            },
            "debug": True,
        },
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="task/actor name, e.g. agent-webhook")
    p.add_argument("prompt", help="what Claude should do in the repo")
    p.add_argument("--branch", help="branch to push (default: ax/<name>)")
    args = p.parse_args()

    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?", args.name):
        sys.exit("error: name must be lowercase letters, digits and dashes")
    branch = args.branch or f"ax/{args.name}-{os.urandom(2).hex()}"
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch in ("master", "main"):
        sys.exit("error: branch must be a plain branch name other than master/main")

    out_dir = SECRETS / "rendered"
    out_dir.mkdir(mode=0o700, exist_ok=True)
    out = out_dir / f"{args.name}.yaml"
    # JSON is valid YAML, and serializing a dict avoids hand-escaping the script.
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(build_task(args.name, args.prompt, branch), f, indent=2)
    print(out)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # no traceback: it could include token-bearing values
        sys.exit(f"error: {type(e).__name__} (details suppressed)")
