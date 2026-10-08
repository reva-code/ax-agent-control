#!/usr/bin/env python3
"""Turn `kubectl ate logs actors` output into a readable agent transcript.

Keeps AX-STEP markers, Claude's text, tool calls and the final result; drops
gVisor/runner noise. Anything token-shaped is masked. Reads stdin.
"""
import json
import re
import sys

MASKS = [
    (re.compile(r"(ghp_|gho_|github_pat_)[A-Za-z0-9_]+"), r"\1***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),
    (re.compile(r"//[^/@\s]+@"), "//***@"),
]


def mask(s):
    for pattern, repl in MASKS:
        s = pattern.sub(repl, s)
    return s


def one_line(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(event):
    """Return a display line for one log record, or None to skip it."""
    msg = event.get("message")
    if isinstance(msg, str):
        if msg.startswith("AX-STEP:"):
            return "▶ " + msg[len("AX-STEP:"):].strip()
        if msg.startswith("AX-SAY:"):
            return "  " + one_line(msg[len("AX-SAY:"):].strip(), 300)
        if "task command exited" in msg:
            return "✗ " + one_line(msg)
        return None
    kind = event.get("type")
    if kind == "assistant" and isinstance(msg, dict):
        lines = []
        for part in msg.get("content", []):
            if part.get("type") == "text":
                lines.append("  Claude: " + one_line(part["text"], 300))
            elif part.get("type") == "tool_use":
                args = part.get("input", {})
                arg = args.get("command") or args.get("file_path") or args.get("pattern") or ""
                lines.append(f"  tool: {part['name']} {one_line(arg, 120)}")
        return "\n".join(lines) or None
    if kind == "result":
        status = "error" if event.get("is_error") else "ok"
        cost = event.get("total_cost_usd")
        return f"  Claude finished ({status}, {event.get('num_turns')} turns, ${cost:.3f})" if cost is not None \
            else f"  Claude finished ({status})"
    return None


def describe_raw(raw):
    """describe() for one raw log line; None if it isn't a JSON record worth showing."""
    try:
        event = json.loads(mask(raw.rstrip("\n")))
    except ValueError:
        return None
    return describe(event) if isinstance(event, dict) else None


if __name__ == "__main__":
    for raw in sys.stdin:
        line = describe_raw(raw)
        if line:
            print(line, flush=True)
