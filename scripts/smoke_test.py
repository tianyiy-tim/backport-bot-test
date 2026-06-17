"""
Pre-flight smoke test for the backport bot.

The bot degrades *silently*: if the Anthropic SDK is missing, AWS creds are
absent, the Bedrock model id is wrong, or `gh` isn't authenticated, the AI and
PR-publishing steps quietly no-op (their exceptions are swallowed). That makes
"the run finished without errors" a misleading signal.

This script validates every runtime precondition up front and FAILS LOUDLY,
so a misconfiguration can't masquerade as "AI intentionally skipped."

Checks:
  1. anthropic SDK importable (AnthropicBedrock available)
  2. AWS credentials present in the environment
  3. Bedrock reachable — performs a tiny live call using the EXACT model id and
     request shape the bot uses (validates model access, region, and the
     `thinking` parameter)
  4. `gh` CLI installed and a GitHub token present (used for PRs + git push)
  5. BACKPORT_REPO set (pins PRs/comments to this repo — critical on a fork)
  6. git available and an `origin` remote is configured

Exit code 0 = ready to run the bot. Non-zero = at least one hard failure.

Run:  python scripts/smoke_test.py
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backport_bot  # noqa: E402

# Outcomes
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results = []  # (check_name, outcome, detail)


def record(name, outcome, detail=""):
    results.append((name, outcome, detail))
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[outcome]
    print(f"{icon} [{outcome}] {name}" + (f"\n        {detail}" if detail else ""))


def check_sdk():
    if backport_bot._anthropic_module is None:
        record(
            "anthropic SDK importable",
            FAIL,
            "`import anthropic` failed. Run `pip install anthropic`.",
        )
        return False
    record("anthropic SDK importable", PASS)
    return True


def check_aws_creds():
    have_key = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
    have_secret = bool(os.environ.get("AWS_SECRET_ACCESS_KEY"))
    if have_key and have_secret:
        extra = " (+ session token)" if os.environ.get("AWS_SESSION_TOKEN") else ""
        record("AWS credentials present", PASS, f"key + secret found{extra}")
        return True
    missing = [
        n
        for n, v in (
            ("AWS_ACCESS_KEY_ID", have_key),
            ("AWS_SECRET_ACCESS_KEY", have_secret),
        )
        if not v
    ]
    record("AWS credentials present", FAIL, f"missing: {', '.join(missing)}")
    return False


def check_bedrock_live():
    """Mirror the bot's exact call so a bad model id / region / param is caught."""
    client = backport_bot._ai_client()
    if client is None:
        record(
            "Bedrock reachable (live ping)",
            FAIL,
            "_ai_client() returned None (SDK missing or AWS_ACCESS_KEY_ID unset).",
        )
        return False
    try:
        with client.messages.stream(
            model=backport_bot._BEDROCK_MODEL_ID,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system="You are a connectivity probe. Reply with a single word.",
            messages=[{"role": "user", "content": "Reply with the word: OK"}],
        ) as stream:
            msg = stream.get_final_message()
        text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        record(
            "Bedrock reachable (live ping)",
            PASS,
            f"model={backport_bot._BEDROCK_MODEL_ID} replied: {text[:40]!r}",
        )
        return True
    except Exception as exc:
        record(
            "Bedrock reachable (live ping)",
            FAIL,
            f"call failed with model={backport_bot._BEDROCK_MODEL_ID} "
            f"region={os.environ.get('AWS_REGION', 'us-east-1')}: {exc}",
        )
        return False


def check_gh():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    gh_ok = False
    try:
        v = subprocess.run(["gh", "--version"], capture_output=True, text=True)
        gh_ok = v.returncode == 0
    except FileNotFoundError:
        gh_ok = False

    if gh_ok and token:
        record("gh CLI + token", PASS, "gh installed and a GitHub token is set")
        return True
    detail = []
    if not gh_ok:
        detail.append("`gh` CLI not found")
    if not token:
        detail.append("GITHUB_TOKEN / GH_TOKEN not set")
    record("gh CLI + token", FAIL, "; ".join(detail))
    return False


def check_backport_repo():
    repo = os.environ.get("BACKPORT_REPO")
    if repo:
        record("BACKPORT_REPO set", PASS, repo)
        return True
    record(
        "BACKPORT_REPO set",
        WARN,
        "unset — on a fork, PRs/comments may default to the UPSTREAM parent. "
        "Set BACKPORT_REPO=<owner>/<repo> to pin them to this fork.",
    )
    return True  # non-fatal, but strongly recommended


def check_git():
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
        if top.returncode != 0:
            record("git repo + origin", FAIL, "not inside a git repository")
            return False
        remotes = subprocess.run(
            ["git", "remote"], capture_output=True, text=True
        ).stdout.split()
        if "origin" not in remotes:
            record("git repo + origin", FAIL, f"no 'origin' remote (have: {remotes})")
            return False
        record("git repo + origin", PASS)
        return True
    except FileNotFoundError:
        record("git repo + origin", FAIL, "git not installed")
        return False


def main():
    print("Backport bot pre-flight smoke test\n" + "=" * 40)
    check_git()
    check_gh()
    check_backport_repo()
    sdk_ok = check_sdk()
    creds_ok = check_aws_creds()
    if sdk_ok and creds_ok:
        check_bedrock_live()
    else:
        record(
            "Bedrock reachable (live ping)",
            FAIL,
            "skipped — SDK and/or AWS credentials missing (see above).",
        )

    print("\n" + "=" * 40)
    n_fail = sum(1 for _, o, _ in results if o == FAIL)
    n_warn = sum(1 for _, o, _ in results if o == WARN)
    if n_fail:
        print(f"RESULT: {n_fail} failure(s), {n_warn} warning(s) — NOT ready. ❌")
        sys.exit(1)
    if n_warn:
        print(f"RESULT: all hard checks passed, {n_warn} warning(s). ⚠️")
        sys.exit(0)
    print("RESULT: all checks passed — ready to run the bot. ✅")
    sys.exit(0)


if __name__ == "__main__":
    main()
