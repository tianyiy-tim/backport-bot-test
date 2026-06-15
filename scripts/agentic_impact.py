"""
Agentic impact analysis — RESEARCH PROTOTYPE.

Purpose
-------
The deterministic analyzer (see backport_bot.py) answers "is the introducing
commit in this branch's history?" using git ancestry. That works for the typical
case but has known blind spots, all of which look the same from git's structural
point of view:

  - a bug introduced partway through a line's history (oldest commit is innocent)
  - a fix layered on top of an earlier fix (introducer is main-only)
  - code that traces back to the BoringSSL mass-import (ancestor of everything)
  - a rename + heavy rewrite that breaks the rename trail

In all of these, deciding whether a branch is *actually* vulnerable needs an
understanding of what the code does, not just how it changed. That is the gap
this prototype explores: an AI agent that investigates a branch and judges
whether the vulnerable pattern is present.

This is intentionally an *agentic* design rather than a single prompt: the model
is given a small set of READ-ONLY tools and lets it gather the context it needs
(read the file on the branch, grep, inspect line history) before deciding. That
suits ambiguous cases, where the right context isn't known in advance.

SECURITY (this is a prototype; do not point it at a real model without review)
------------------------------------------------------------------------------
This sends source code to a model, so the AppSec GitHub-agent guidance applies.
The design encodes the guardrails up front:

  - Tools are strictly READ-ONLY. The agent cannot write, push, cherry-pick,
    merge, or run arbitrary shell. It can only run a fixed set of git reads.
  - The agent's verdict is ADVISORY. It only ever runs on branches the
    deterministic pass already marked "not affected", and can only escalate them
    to "needs a human look". It can never suppress a deterministic finding.
  - Output is structured and validated against a fixed set of verdicts.
  - Iterations are bounded, so a manipulated model cannot loop forever.
  - Only the minimal code needed is exposed via the tools, never secrets.

The model call itself (`call_model`) is MOCKED. To use a real model, implement
that one function against an approved/internal provider.
"""

import json
import re
import subprocess

MAX_STEPS = 6  # bound the agent loop so it can't run away

VALID_VERDICTS = {"affected", "not_affected", "uncertain"}


# ---------------------------------------------------------------------------
# Read-only tools the agent is allowed to call.
# Each returns a string. None of them mutate repo state.
# ---------------------------------------------------------------------------


def tool_get_fix_diff(commit):
    """Return the diff the fix introduced (commit vs its parent)."""
    result = subprocess.run(
        ["git", "show", "--format=%s%n%n%b", commit],
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else f"ERROR: {result.stderr}"


def tool_read_file_on_branch(branch, path):
    """
    Return the contents of `path` on `branch`. Falls back to the file's
    basename if the exact path isn't present (handles renames/moves).
    """
    result = subprocess.run(
        ["git", "show", f"origin/{branch}:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout
    # Fallback: the file may live at a different path on this branch.
    basename = path.rsplit("/", 1)[-1]
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", f"origin/{branch}"],
        capture_output=True,
        text=True,
    )
    if listing.returncode == 0:
        for candidate in listing.stdout.splitlines():
            if candidate.rsplit("/", 1)[-1] == basename:
                alt = subprocess.run(
                    ["git", "show", f"origin/{branch}:{candidate}"],
                    capture_output=True,
                    text=True,
                )
                if alt.returncode == 0:
                    return f"(found at {candidate})\n{alt.stdout}"
    return f"FILE NOT PRESENT on {branch}: {path}"


def tool_grep_branch(branch, pattern):
    """Search the branch's tree for a regex pattern (read-only)."""
    result = subprocess.run(
        ["git", "grep", "-n", "-E", pattern, f"origin/{branch}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout[:4000]  # cap output size
    return f"(no matches for /{pattern}/ on {branch})"


TOOLS = {
    "get_fix_diff": tool_get_fix_diff,
    "read_file_on_branch": tool_read_file_on_branch,
    "grep_branch": tool_grep_branch,
}


# ---------------------------------------------------------------------------
# The model call. MOCKED for the prototype.
# ---------------------------------------------------------------------------


def call_model(system_prompt, transcript):
    """
    Stand-in for a real LLM call.

    A real implementation would send `system_prompt` + `transcript` to an
    approved model with the tool schema, and return either:
      - {"action": "tool", "tool": <name>, "args": {...}}   (agent wants data)
      - {"action": "final", "verdict": ..., "confidence": ..., "reasoning": ...}

    This mock simulates a plausible agent trajectory deterministically so the
    pipeline runs end-to-end without a model: it reads the fix, reads the file
    on the branch, then judges presence of the vulnerable pattern with a crude
    heuristic. The heuristic is NOT real semantic analysis; it only exists so
    the harness produces realistic-shaped output.
    """
    # Figure out what the agent has already gathered.
    have_fix = any(t.get("tool") == "get_fix_diff" for t in transcript)
    have_file = any(t.get("tool") == "read_file_on_branch" for t in transcript)

    ctx = _mock_context  # populated by evaluate_branch before the loop

    if not have_fix:
        return {
            "action": "tool",
            "tool": "get_fix_diff",
            "args": {"commit": ctx["commit"]},
        }
    if not have_file:
        return {
            "action": "tool",
            "tool": "read_file_on_branch",
            "args": {"branch": ctx["branch"], "path": ctx["file"]},
        }

    # The agent now has the fix and the branch's version of the file. Decide.
    branch_code = ""
    for t in transcript:
        if t.get("tool") == "read_file_on_branch":
            branch_code = t.get("result", "")

    if branch_code.startswith("FILE NOT PRESENT"):
        return {
            "action": "final",
            "verdict": "not_affected",
            "confidence": "high",
            "reasoning": "The patched file does not exist on this branch, so the "
            "vulnerable code path is not present.",
        }

    # Crude stand-in for semantic judgement: does the patched function exist on
    # the branch, and does it lack the guard the fix adds?
    func = ctx.get("function")
    guard = ctx.get("fix_guard")
    if func and func in branch_code:
        if guard and guard in branch_code:
            return {
                "action": "final",
                "verdict": "not_affected",
                "confidence": "medium",
                "reasoning": f"`{func}` exists on the branch but already contains "
                f"the guard introduced by the fix, so it appears patched.",
            }
        return {
            "action": "final",
            "verdict": "affected",
            "confidence": "medium",
            "reasoning": f"`{func}` exists on the branch and lacks the guard the fix "
            f"adds, so the vulnerable pattern is likely present even though "
            f"ancestry did not flag it.",
        }

    return {
        "action": "final",
        "verdict": "uncertain",
        "confidence": "low",
        "reasoning": "Could not locate the patched function on the branch; a human "
        "should confirm whether the vulnerable behavior exists here.",
    }


# Shared context the mock reads (a real model would get this in the prompt).
_mock_context = {}


# ---------------------------------------------------------------------------
# The agent loop.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a security assistant helping decide whether a release
branch is affected by a vulnerability that a deterministic ancestry check could
not confirm. You may call read-only tools to inspect the branch. When you have
enough information, return a final verdict of "affected", "not_affected", or
"uncertain" with a short reasoning. You cannot modify anything; your verdict is
advisory and will be reviewed by a human."""


def evaluate_branch(commit, branch, file, function=None, fix_guard=None):
    """
    Run the agentic evaluation for one (commit, branch). Returns a validated
    verdict dict. `function` and `fix_guard` are hints the mock uses; a real
    model would infer these from the fix diff.
    """
    global _mock_context
    _mock_context = {
        "commit": commit,
        "branch": branch,
        "file": file,
        "function": function,
        "fix_guard": fix_guard,
    }

    transcript = []
    for _step in range(MAX_STEPS):
        decision = call_model(SYSTEM_PROMPT, transcript)

        if decision["action"] == "tool":
            name = decision["tool"]
            args = decision["args"]
            if name not in TOOLS:
                # A real model could hallucinate a tool name; reject it.
                transcript.append(
                    {"tool": name, "result": f"ERROR: unknown tool {name}"}
                )
                continue
            result = TOOLS[name](**args)
            transcript.append({"tool": name, "args": args, "result": result})
            continue

        if decision["action"] == "final":
            verdict = decision.get("verdict")
            if verdict not in VALID_VERDICTS:
                # Reject malformed output rather than trusting it.
                return {
                    "verdict": "uncertain",
                    "confidence": "low",
                    "reasoning": f"Model returned an invalid verdict: {verdict!r}",
                    "steps": len(transcript),
                }
            return {
                "verdict": verdict,
                "confidence": decision.get("confidence", "unknown"),
                "reasoning": decision.get("reasoning", ""),
                "steps": len(transcript),
            }

    # Ran out of steps without a verdict.
    return {
        "verdict": "uncertain",
        "confidence": "low",
        "reasoning": f"No verdict within {MAX_STEPS} steps; flag for human review.",
        "steps": len(transcript),
    }
