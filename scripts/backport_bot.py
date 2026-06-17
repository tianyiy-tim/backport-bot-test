"""
Backport Bot - Automated cherry-pick backporting for AWS-LC

Given a merged commit on main, this bot:
1. Resolves which branches are currently supported
2. Determines which of those branches are affected
3. Cherry-picks the fix to affected branches
4. Opens PRs for human review (or flags failures)
"""

import json
import os
import re
import subprocess
import sys

try:
    import anthropic as _anthropic_module
except ImportError:
    _anthropic_module = None

_AI_MAX_DIFF_BYTES = 40_000  # cap diff bytes fed to the model
_AI_MAX_FILE_BYTES = 15_000  # cap per-file context bytes fed to the model

# Bedrock cross-region inference profile for Claude Opus 4.8.
# Verify the exact ID in the AWS Bedrock console under "Cross-region inference".
# Overridable via env so you can try alternates (e.g. "us.anthropic.claude-opus-4-8")
# without editing code.
_BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-8-20251101-v1:0"
)

# --- Configuration ---

# Branches we treat as "supported" for backport purposes.
# Each entry is a prefix matched against `origin/<branch>` from `git branch -r`.
# Overridable per-repo via the BACKPORT_BRANCH_PREFIXES env var (comma-separated).
#
# Default covers both:
#   - real aws/aws-lc release branches: `fips-YYYY-MM-DD` (incl. `fips-NetOS-*`)
#   - the synthetic POC fixture:        `AWS-LC-FIPS-*` and `NetOS`
SUPPORTED_BRANCH_PREFIXES = tuple(
    p.strip()
    for p in os.environ.get(
        "BACKPORT_BRANCH_PREFIXES",
        "origin/fips-,origin/AWS-LC-FIPS-,origin/NetOS",
    ).split(",")
    if p.strip()
)

# TODO: Add FIPS boundary detection later
# FIPS_BOUNDARY_PATHS = ["crypto/fipsmodule/"]


# --- Step 1: Branch Resolver ---


def get_supported_branches():
    """
    Get list of currently supported branches.
    - Query remote branches matching naming conventions (AWS-LC-FIPS-* + NetOS)
    - Filter by end-of-support dates from VERSIONING.md  (skip for now)
    - Return list of branch names (without the "origin/" prefix)
    """
    result = subprocess.run(["git", "branch", "-r"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git branch -r failed: {result.stderr}")

    branches = []
    for line in result.stdout.splitlines():
        line = line.strip()
        # Skip the symbolic ref "origin/HEAD -> origin/main".
        if " -> " in line:
            continue
        if not line.startswith(SUPPORTED_BRANCH_PREFIXES):
            continue
        branches.append(line[len("origin/") :])
    return branches


# --- Step 2: Impact Analyzer ---


def get_changed_files(commit):
    """
    Get list of files changed by the fix commit.
    - Use git diff to compare commit with its parent
    - Return list of file paths
    """

    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff-tree failed: {result.stderr}")

    files = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        files.append(line)

    return files


def find_introducing_commit(commit, files):
    """
    Find which commit(s) introduced the code that the fix is changing.

    Strategy:
    - For each file, diff the fix against its parent to find which line ranges
      were touched.
    - For each line range, ask git for the FULL chain of commits that ever
      modified those lines (`git log -L ... --reverse`). The OLDEST commit in
      the chain is when those exact lines first appeared in history — that's
      the introducing commit.
    - Falls back to `git blame` (with whitespace/move-aware flags) if log -L
      returns nothing, since some edge cases (e.g. files added in the very
      first commit) trip up log -L's diff machinery.

    Returns: set of introducing commit SHAs.
    """
    introducing = set()

    for file in files:
        # 1. Diff the fix vs. its parent for this file (no context lines).
        result = subprocess.run(
            ["git", "diff", "-U0", f"{commit}^", commit, "--", file],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr}")

        # 2. Walk each hunk header (@@ -A,B +C,D @@) and decide which lines to inspect.
        for line in result.stdout.splitlines():
            if not line.startswith("@@"):
                continue
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? ", line)
            if not match:
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) else 1

            if old_count == 0:
                # Pure addition: inspect the line right after the insertion point.
                blame_start = old_start + 1
                blame_end = old_start + 1
            else:
                # Lines were removed/modified: inspect those exact lines.
                blame_start = old_start
                blame_end = old_start + old_count - 1

            origin_sha = _find_line_origin(file, blame_start, blame_end, f"{commit}^")
            if origin_sha:
                introducing.add(origin_sha)

    return introducing


def _find_line_origin(file, line_start, line_end, ref):
    """
    Return the SHA of the OLDEST commit that touched lines [line_start, line_end]
    of `file` as seen from `ref`. That is, when those exact lines first appeared.

    Uses `git log -L<start>,<end>:<file> --reverse --format=%H ref` which walks
    the line history from oldest to newest. The first SHA in the output is the
    commit where those lines were originally introduced.

    Falls back to `git blame -w -M -C` if log -L returns nothing.
    """
    log_result = subprocess.run(
        [
            "git",
            "log",
            f"-L{line_start},{line_end}:{file}",
            "--format=%H",
            "--reverse",
            ref,
        ],
        capture_output=True,
        text=True,
    )
    if log_result.returncode == 0:
        for log_line in log_result.stdout.splitlines():
            log_line = log_line.strip()
            # `--format=%H` only prints SHAs on their own lines; the rest is the
            # diff body. Take the first 40-char hex string we see.
            if len(log_line) == 40 and all(c in "0123456789abcdef" for c in log_line):
                return log_line

    # Fallback: use blame (with whitespace/move-aware flags). Less accurate for
    # finding the original introducer, but works on edge cases log -L can't.
    blame_result = subprocess.run(
        [
            "git",
            "blame",
            "-w",
            "-M",
            "-C",
            "-L",
            f"{line_start},{line_end}",
            ref,
            "--",
            file,
        ],
        capture_output=True,
        text=True,
    )
    if blame_result.returncode != 0:
        raise RuntimeError(
            f"both git log -L and git blame failed for {file}:{line_start}-{line_end} "
            f"on {ref}: {blame_result.stderr}"
        )
    for blame_line in blame_result.stdout.splitlines():
        if not blame_line:
            continue
        return blame_line.split()[0].lstrip("^")
    return None


def is_branch_affected(
    introducing_commits, branch, commit=None, changed_files=None
) -> "tuple[bool, dict | None]":
    """
    Check if the branch contains the vulnerable code.

    Three paths:
    1. Direct ancestry — introducer SHA is in the branch's history.
    2. Cherry-pick equivalence — the introducer's *content* (patch-id) matches
       a commit on the branch, even if the SHA is different. This catches the
       common case where a feature was cherry-picked to a release branch and
       got a new SHA in the process.
    3. AI advisory (fallback) — if both deterministic paths are inconclusive,
       call Claude for an advisory assessment. The result is recorded but the
       function still returns False so that the human-reviewed backport PR is
       opened only when deterministic evidence exists. The advisory text is
       attached to the summary comment separately.

    Returns (affected: bool, ai_advisory: dict | None).
    """
    ref = f"origin/{branch}"

    # Path 1: direct SHA ancestry (fast)
    for sha in introducing_commits:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, ref],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True, None
        if result.returncode != 1:
            raise RuntimeError(
                f"git merge-base failed (code {result.returncode}) "
                f"checking {sha} against {ref}: {result.stderr}"
            )

    # Path 2: patch-id equivalence (handles cherry-picked introducers)
    branch_pids = _get_branch_patch_ids(ref)
    for sha in introducing_commits:
        pid = _patch_id_of(sha)
        if pid and pid in branch_pids:
            return True, None

    # Path 3: AI advisory when both deterministic paths are inconclusive.
    # We still return False so that the bot does not auto-backport on AI
    # reasoning alone. The advisory is surfaced in the summary comment.
    if commit and changed_files:
        advisory = ai_impact_analysis(
            commit, branch, changed_files, introducing_commits
        )
        if advisory is not None:
            print(
                f"[ai] impact analysis for {branch}: "
                f"likely_affected={advisory['likely_affected']}, "
                f"confidence={advisory['confidence']}",
                file=sys.stderr,
            )
            return False, advisory

    return False, None


def _get_branch_patch_ids(ref):
    """
    Return the set of patch-ids for the branch's DIVERGENT commits — those on
    `ref` but not on mainline. Cherry-picked backports live there, so this is
    the right (and far smaller) set to scan than the branch's entire history.

    Reads git output as bytes to tolerate non-UTF-8 content (e.g. binary test
    vectors in the diffs), which would otherwise crash text decoding.

    Mainline ref is configurable via BACKPORT_MAINLINE_REF (default origin/main).
    """
    mainline = os.environ.get("BACKPORT_MAINLINE_REF", "origin/main")
    rev_range = f"{mainline}..{ref}"
    log = subprocess.run(
        ["git", "log", "-p", "--no-merges", "--format=%H", rev_range],
        capture_output=True,  # bytes, not text: diffs may contain binary content
    )
    if log.returncode != 0:
        return set()
    pid_proc = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=log.stdout,
        capture_output=True,
    )
    if pid_proc.returncode != 0:
        return set()
    out = pid_proc.stdout.decode("ascii", errors="replace")
    return {line.split()[0] for line in out.splitlines() if line.split()}


def is_already_patched(commit, branch):
    """
    Check whether the change in `commit` has already been applied to `branch`,
    even with a different SHA (i.e. via a manual cherry-pick).

    Uses `git patch-id`, which produces a stable hash from a patch's content
    (ignoring commit metadata, line numbers in headers, etc.). Two commits with
    the same logical changes produce the same patch-id, regardless of SHA.

    Algorithm:
    1. Compute patch-id of the fix commit.
    2. Compute patch-ids of the branch's divergent commits.
    3. Return True if any match.
    """
    target_pid = _patch_id_of(commit)
    if not target_pid:
        # patch-id can be empty for trivial/edge-case patches; assume not patched.
        return False

    branch_pids = _get_branch_patch_ids(f"origin/{branch}")
    return target_pid in branch_pids


def _patch_id_of(commit):
    """Return the patch-id (content hash) of a single commit, or None on failure."""
    show = subprocess.run(
        ["git", "show", commit],
        capture_output=True,  # bytes: the commit may touch binary files
    )
    if show.returncode != 0:
        return None
    pid = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=show.stdout,
        capture_output=True,
    )
    if pid.returncode != 0 or not pid.stdout.strip():
        return None
    return pid.stdout.decode("ascii", errors="replace").split()[0]


# --- AI Advisory Functions ---
# These functions call the Claude API to surface advisory analysis in PR
# comments. They NEVER modify code or commit anything — all output is text
# embedded in PR descriptions / summary comments for human review.


def _ai_client():
    """Return an AnthropicBedrock client if the SDK and AWS credentials are available, else None."""
    if _anthropic_module is None:
        return None
    region = os.environ.get("AWS_REGION", "us-east-1")
    # Credentials are picked up automatically from the environment:
    # AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ optional AWS_SESSION_TOKEN)
    # or an IAM role attached to the Actions runner.
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        return None
    return _anthropic_module.AnthropicBedrock(aws_region=region)


def _get_commit_diff(commit):
    """Return the full diff for *commit* as a string (capped at _AI_MAX_DIFF_BYTES)."""
    result = subprocess.run(
        ["git", "show", "--stat", "-p", commit],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout[:_AI_MAX_DIFF_BYTES]


def _get_file_on_branch(file_path, branch_ref):
    """Return the contents of *file_path* as it exists on *branch_ref* (capped)."""
    result = subprocess.run(
        ["git", "show", f"{branch_ref}:{file_path}"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout[:_AI_MAX_FILE_BYTES]


def ai_impact_analysis(commit, branch, changed_files, introducing_commits):
    """
    Advisory: ask Claude whether *branch* is likely affected by the vulnerability
    addressed in *commit*, given that deterministic ancestry checks were
    inconclusive.

    Returns a dict:
      {
        "likely_affected": True | False | None,   # None = model uncertain
        "confidence": "high" | "medium" | "low",
        "reasoning": "<short prose>",
        "raw_advisory": "<full advisory text for PR comment>"
      }

    Output is ADVISORY ONLY — never auto-applied or used to skip backports.
    """
    client = _ai_client()
    if client is None:
        return None

    fix_diff = _get_commit_diff(commit)

    # Collect relevant file snapshots from the branch
    file_context_parts = []
    branch_ref = f"origin/{branch}"
    for f in changed_files[:6]:  # limit number of files to control prompt size
        content = _get_file_on_branch(f, branch_ref)
        if content:
            file_context_parts.append(f"### {f} (on {branch})\n```\n{content}\n```")
    file_context = (
        "\n\n".join(file_context_parts) if file_context_parts else "(not available)"
    )

    introducer_list = ", ".join(list(introducing_commits)[:5]) or "(none found)"

    system = (
        "You are a security-focused code review assistant integrated into an "
        "automated CVE backport pipeline for AWS-LC (Amazon's cryptographic library). "
        "Your task is to assess whether a specific release branch is affected by a "
        "vulnerability that was fixed on main.\n\n"
        "IMPORTANT CONSTRAINTS:\n"
        "- Your analysis is ADVISORY ONLY. It will be surfaced in a GitHub PR comment "
        "for human review and must never be automatically applied or acted on.\n"
        "- Do not speculate beyond what the code evidence shows.\n"
        "- If the diff or file contents are truncated or unclear, say so and lower your "
        "confidence accordingly.\n"
        "- Output must be plain Markdown suitable for a GitHub comment."
    )

    user = (
        f"## Impact Analysis Request\n\n"
        f"**Fix commit:** `{commit}`\n"
        f"**Target branch:** `{branch}`\n"
        f"**Introducing commit(s):** {introducer_list}\n\n"
        f"### Patch diff (what the fix changes on main)\n"
        f"```diff\n{fix_diff}\n```\n\n"
        f"### Relevant files on the target branch\n"
        f"{file_context}\n\n"
        f"---\n"
        f"Deterministic ancestry checks (SHA ancestry and patch-id matching) were "
        f"inconclusive for this branch. Please assess:\n\n"
        f"1. Does the branch likely contain the vulnerable code shown in the diff?\n"
        f"2. If so, does the fix apply cleanly in spirit (even if a cherry-pick "
        f"conflicts due to diverged context)?\n"
        f"3. What is your confidence level (high/medium/low) and why?\n\n"
        f"Respond with:\n"
        f"- **Likely affected**: Yes / No / Uncertain\n"
        f"- **Confidence**: high / medium / low\n"
        f"- **Reasoning**: 2-4 sentences\n"
        f"- **Recommendation**: brief action for the human reviewer"
    )

    try:
        with client.messages.stream(
            model=_BEDROCK_MODEL_ID,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            response = stream.get_final_message()
    except Exception as exc:
        print(f"[ai_impact_analysis] API call failed: {exc}", file=sys.stderr)
        return None

    raw = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()

    # Parse the structured fields from the model's response
    likely = None
    confidence = "low"
    for line in raw.splitlines():
        ll = line.lower()
        if "likely affected" in ll:
            if "yes" in ll:
                likely = True
            elif "no" in ll:
                likely = False
            # else: leave as None (uncertain)
        if "confidence" in ll:
            for level in ("high", "medium", "low"):
                if level in ll:
                    confidence = level
                    break

    return {
        "likely_affected": likely,
        "confidence": confidence,
        "reasoning": raw,
        "raw_advisory": (
            f"<details>\n"
            f"<summary>🤖 AI Impact Analysis (advisory — not auto-applied)</summary>\n\n"
            f"{raw}\n\n"
            f"</details>"
        ),
    }


def ai_conflict_resolution(commit, branch, conflict_output):
    """
    Advisory: ask Claude to propose how to resolve a cherry-pick conflict.

    *conflict_output* is the stderr/stdout captured from the failed cherry-pick.

    Returns a dict:
      {
        "proposed_resolution": "<prose + diff suggestion>",
        "raw_advisory": "<full advisory text for PR description>"
      }

    Output is ADVISORY ONLY — pasted into the PR body for a human to apply by hand.
    It is never committed or auto-applied.
    """
    client = _ai_client()
    if client is None:
        return None

    fix_diff = _get_commit_diff(commit)

    # Grab the conflicting files' current state on the branch
    conflicted_files = []
    for line in conflict_output.splitlines():
        # git cherry-pick --abort output mentions "CONFLICT (content): Merge conflict in <file>"
        m = re.search(r"Merge conflict in (.+)$", line)
        if m:
            conflicted_files.append(m.group(1).strip())

    file_context_parts = []
    branch_ref = f"origin/{branch}"
    for f in conflicted_files[:4]:
        content = _get_file_on_branch(f, branch_ref)
        if content:
            file_context_parts.append(
                f"### {f} (on {branch} before cherry-pick)\n```\n{content}\n```"
            )
    file_context = (
        "\n\n".join(file_context_parts) if file_context_parts else "(not available)"
    )

    system = (
        "You are a security-focused code review assistant integrated into an "
        "automated CVE backport pipeline for AWS-LC (Amazon's cryptographic library). "
        "A cherry-pick to a release branch failed with conflicts. Your job is to "
        "propose a resolution that a human engineer can review, adjust, and apply.\n\n"
        "IMPORTANT CONSTRAINTS:\n"
        "- Your resolution is ADVISORY ONLY. It will be pasted into a GitHub PR "
        "description for human review. A human must manually apply and verify it.\n"
        "- Do not invent logic not present in the original patch.\n"
        "- If context is insufficient to be confident, say so explicitly.\n"
        "- Output must be plain Markdown suitable for a GitHub PR description."
    )

    user = (
        f"## Conflict Resolution Request\n\n"
        f"**Fix commit:** `{commit}`\n"
        f"**Target branch:** `{branch}`\n\n"
        f"### Cherry-pick conflict output\n"
        f"```\n{conflict_output[:3000]}\n```\n\n"
        f"### Original patch diff\n"
        f"```diff\n{fix_diff}\n```\n\n"
        f"### Conflicting files on the target branch\n"
        f"{file_context}\n\n"
        f"---\n"
        f"Please:\n"
        f"1. Identify the nature of each conflict (context drift, renamed symbols, "
        f"structural divergence, etc.).\n"
        f"2. Propose a concrete resolution for each conflicted file as a diff or "
        f"annotated code block.\n"
        f"3. Flag any areas where you are uncertain and the human reviewer must exercise "
        f"independent judgment.\n\n"
        f"Format each conflict as:\n"
        f"**File: `<path>`**\n"
        f"- Conflict type: ...\n"
        f"- Proposed resolution:\n"
        f"```diff\n...\n```\n"
        f"- Reviewer note: ..."
    )

    try:
        with client.messages.stream(
            model=_BEDROCK_MODEL_ID,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            response = stream.get_final_message()
    except Exception as exc:
        print(f"[ai_conflict_resolution] API call failed: {exc}", file=sys.stderr)
        return None

    raw = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()

    return {
        "proposed_resolution": raw,
        "raw_advisory": (
            f"<details>\n"
            f"<summary>🤖 AI Conflict Resolution Suggestion (advisory — apply manually after review)</summary>\n\n"
            f"{raw}\n\n"
            f"⚠️ **This suggestion was generated by an AI. It must be reviewed and "
            f"applied by a human engineer. Never merge without independent verification.**\n\n"
            f"</details>"
        ),
    }


# --- Step 3: Pre-Flight Checks ---
# TODO: Add FIPS boundary detection here later


# --- Step 4: Backport Engine ---


def cherry_pick_to_branch(commit, branch):
    """
    Attempt to cherry-pick `commit` onto `branch`.
    Returns ("success", new_branch_name) or ("conflict", conflict_output_str).
    """
    # 1. Save the current branch name so we can return to it.
    saved = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if saved.returncode != 0:
        raise RuntimeError(f"failed to read current branch: {saved.stderr}")
    original_branch = saved.stdout.strip()

    # 2. Resolve `commit` (could be a tag, branch, or short SHA) to a full SHA.
    resolved = subprocess.run(
        ["git", "rev-parse", commit],
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise RuntimeError(f"failed to resolve {commit}: {resolved.stderr}")
    full_sha = resolved.stdout.strip()

    # 3. Build a name for the new backport branch.
    short_sha = full_sha[:8]
    new_branch = f"backport/{branch}/{short_sha}"

    # 4. If a previous run left this branch behind, wipe it before recreating.
    #    Don't check returncode — if the branch doesn't exist, that's fine.
    subprocess.run(
        ["git", "branch", "-D", new_branch],
        capture_output=True,
        text=True,
    )

    # 5. Create the new branch from the target.
    #    Note: we use `origin/<branch>` to start from the *remote* tip,
    #    in case the local branch is stale. This matches what CI will do.
    create = subprocess.run(
        ["git", "checkout", "-b", new_branch, f"origin/{branch}"],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        raise RuntimeError(f"failed to create branch: {create.stderr}")

    # 6. Try to cherry-pick.
    pick = subprocess.run(
        ["git", "cherry-pick", commit],
        capture_output=True,
        text=True,
    )

    # 7. Branch on the outcome.
    if pick.returncode == 0:
        # Clean apply.
        # 7a. Push the new branch to origin so GitHub can see it (required
        #     before opening a PR against it).
        push = subprocess.run(
            ["git", "push", "--force", "origin", new_branch],
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            raise RuntimeError(f"failed to push {new_branch}: {push.stderr}")

        # 7b. Switch back to where we were, leave the new branch.
        subprocess.run(
            ["git", "checkout", original_branch], capture_output=True, text=True
        )
        return ("success", new_branch)

    if pick.returncode == 1:
        # Conflict. Capture output for AI advisory, then abort and clean up.
        conflict_output = (pick.stdout + pick.stderr).strip()
        subprocess.run(
            ["git", "cherry-pick", "--abort"], capture_output=True, text=True
        )
        subprocess.run(
            ["git", "checkout", original_branch], capture_output=True, text=True
        )
        subprocess.run(
            ["git", "branch", "-D", new_branch], capture_output=True, text=True
        )
        return ("conflict", conflict_output)

    # Anything else: best-effort cleanup, then raise.
    subprocess.run(["git", "cherry-pick", "--abort"], capture_output=True, text=True)
    subprocess.run(["git", "checkout", original_branch], capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", new_branch], capture_output=True, text=True)
    raise RuntimeError(f"cherry-pick failed: {pick.stderr}")


# --- Step 5: PR Creation & Summary ---


def open_pr(pr_branch, target_branch, commit, pr_number, repo=None):
    """
    Open a pull request from the backport branch to the target branch.
    - Uses the `gh` CLI under the hood.
    - Idempotent: if a PR for this head/base pair already exists, returns
      its URL instead of failing.
    - Returns the URL of the (new or existing) PR.

    `repo` (or the BACKPORT_REPO env var) pins the PR to a specific repository,
    e.g. "tianyiy-tim/aws-lc". THIS IS A SAFETY GUARD: on a fork, `gh pr create`
    defaults the base to the upstream parent (e.g. aws/aws-lc). Pinning the repo
    guarantees backport PRs are created within the fork and never against
    upstream.
    """
    repo = repo or os.environ.get("BACKPORT_REPO")
    repo_args = ["--repo", repo] if repo else []

    # 1. Check whether an open PR for this head/base pair already exists.
    existing = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            *repo_args,
            "--head",
            pr_branch,
            "--base",
            target_branch,
            "--state",
            "open",
            "--json",
            "url",
        ],
        capture_output=True,
        text=True,
    )
    if existing.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {existing.stderr}")

    matches = json.loads(existing.stdout)
    if matches:
        # Already open — reuse it.
        return matches[0]["url"]

    # 2. No existing PR — create a new one.
    title = f"[Backport {target_branch}] backport of #{pr_number}"
    body = (
        f"Automated backport of #{pr_number} (commit `{commit}`).\n\n"
        f"Please review and merge if appropriate."
    )

    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            *repo_args,
            "--base",
            target_branch,
            "--head",
            pr_branch,
            "--title",
            title,
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr}")

    # `gh pr create` prints the new PR's URL on its own line.
    return result.stdout.strip()


def post_summary(pr_number, results, repo=None):
    """
    Post a summary comment on the original PR listing what happened on each branch.

    `results` is a list of dicts with keys:
      branch, status, url, ai_impact, ai_conflict
    (ai_impact and ai_conflict are advisory dicts or None)

    `repo` (or the BACKPORT_REPO env var) pins the comment to a specific
    repository. SAFETY GUARD: without it, `gh pr comment <number>` is resolved
    against whatever repo context gh infers, which on a fork can point at the
    upstream parent. Pinning guarantees the comment lands on the intended repo.
    """
    repo = repo or os.environ.get("BACKPORT_REPO")
    repo_args = ["--repo", repo] if repo else []

    marker = {
        "success": "[OK]",
        "conflict": "[!!]",
        "not_affected": "[>>]",
        "already_patched": "[==]",
        "not_affected_ai": "[??]",
    }

    lines = [f"**Backport summary for #{pr_number}**", ""]
    advisory_blocks = []

    for item in results:
        branch = item["branch"]
        status = item["status"]
        url = item.get("url")
        ai_impact = item.get("ai_impact")
        ai_conflict = item.get("ai_conflict")

        symbol = marker.get(status, "?")
        if status == "success":
            lines.append(f"- {symbol} `{branch}` \u2014 {url}")
        elif status == "conflict":
            lines.append(
                f"- {symbol} `{branch}` \u2014 conflict, manual backport needed"
            )
        elif status == "not_affected":
            lines.append(f"- {symbol} `{branch}` \u2014 not affected")
        elif status == "not_affected_ai":
            lines.append(
                f"- {symbol} `{branch}` \u2014 not affected (deterministic); "
                f"AI advisory attached below"
            )
        elif status == "already_patched":
            lines.append(
                f"- {symbol} `{branch}` \u2014 fix already applied (matching patch-id)"
            )
        else:
            lines.append(f"- {symbol} `{branch}` \u2014 {status}")

        if ai_impact:
            advisory_blocks.append(
                f"#### AI Impact Analysis \u2014 `{branch}`\n\n"
                + ai_impact["raw_advisory"]
            )
        if ai_conflict:
            advisory_blocks.append(
                f"#### AI Conflict Resolution \u2014 `{branch}`\n\n"
                + ai_conflict["raw_advisory"]
            )

    if advisory_blocks:
        lines.append("")
        lines.append("---")
        lines.append(
            "> The following sections contain AI-generated advisory content. "
            "They are informational only and must be reviewed by a human engineer "
            "before any action is taken."
        )
        lines.append("")
        lines.extend(advisory_blocks)

    body = "\n".join(lines)

    result = subprocess.run(
        ["gh", "pr", "comment", *repo_args, str(pr_number), "--body", body],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr comment failed: {result.stderr}")


# --- Main ---


def main():
    commit = os.environ["MERGE_COMMIT"]
    pr_number = int(os.environ["PR_NUMBER"])

    files = get_changed_files(commit)
    introducers = find_introducing_commit(commit, files)
    branches = get_supported_branches()

    results = []
    for branch in branches:
        affected, ai_impact = is_branch_affected(
            introducers, branch, commit=commit, changed_files=files
        )
        if not affected:
            status = "not_affected_ai" if ai_impact else "not_affected"
            results.append(
                {
                    "branch": branch,
                    "status": status,
                    "url": None,
                    "ai_impact": ai_impact,
                    "ai_conflict": None,
                }
            )
            continue

        # Even if the branch contains the introducing commit, the fix may have
        # already been applied via a manual cherry-pick (different SHA, same
        # content). Skip those branches — a backport would be redundant.
        if is_already_patched(commit, branch):
            results.append(
                {
                    "branch": branch,
                    "status": "already_patched",
                    "url": None,
                    "ai_impact": None,
                    "ai_conflict": None,
                }
            )
            continue

        status, payload = cherry_pick_to_branch(commit, branch)
        if status == "success":
            url = open_pr(payload, branch, commit, pr_number)
            results.append(
                {
                    "branch": branch,
                    "status": "success",
                    "url": url,
                    "ai_impact": None,
                    "ai_conflict": None,
                }
            )
        else:
            # payload is the conflict output string captured during the failed pick.
            ai_conflict = ai_conflict_resolution(commit, branch, payload or "")
            results.append(
                {
                    "branch": branch,
                    "status": "conflict",
                    "url": None,
                    "ai_impact": None,
                    "ai_conflict": ai_conflict,
                }
            )

    post_summary(pr_number, results)


if __name__ == "__main__":
    main()
