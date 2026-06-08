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

# --- Configuration ---

# Branches we treat as "supported" for backport purposes.
# Each entry is a prefix matched against `origin/<branch>` from `git branch -r`.
# Add new release lines or one-off branches (like NetOS) here.
SUPPORTED_BRANCH_PREFIXES = (
    "origin/AWS-LC-FIPS-",  # standard release branches
    "origin/NetOS",  # one-off branch for NetOS team (per design doc)
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


def is_branch_affected(introducing_commits, branch):
    """
    Check if the branch contains the vulnerable code.
    - Use git merge-base --is-ancestor to check if any introducing
      commit is in the branch's history
    - Return True if affected, False otherwise
    """
    # We query against `origin/<branch>` because in CI, only the currently
    # checked-out branch exists locally — other release branches only have
    # remote-tracking refs. Locally this also works (origin/<branch> exists
    # alongside the local copy).
    ref = f"origin/{branch}"
    for sha in introducing_commits:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, ref],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            raise RuntimeError(
                f"git merge-base failed (code {result.returncode}) "
                f"checking {sha} against {ref}: {result.stderr}"
            )
    return False


def is_already_patched(commit, branch):
    """
    Check whether the change in `commit` has already been applied to `branch`,
    even with a different SHA (i.e. via a manual cherry-pick).

    Uses `git patch-id`, which produces a stable hash from a patch's content
    (ignoring commit metadata, line numbers in headers, etc.). Two commits with
    the same logical changes produce the same patch-id, regardless of SHA.

    Algorithm:
    1. Compute patch-id of the fix commit.
    2. Compute patch-ids of every non-merge commit on the branch.
    3. Return True if any match.
    """
    # 1. Patch-id of the fix commit.
    target_pid = _patch_id_of(commit)
    if not target_pid:
        # patch-id can be empty for trivial/edge-case patches; assume not patched.
        return False

    # 2. All patch-ids on the branch, computed in a single git pipeline
    #    (efficient even for branches with thousands of commits).
    ref = f"origin/{branch}"
    log = subprocess.run(
        ["git", "log", "-p", "--no-merges", "--format=%H", ref],
        capture_output=True,
        text=True,
    )
    if log.returncode != 0:
        return False

    pid_proc = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=log.stdout,
        capture_output=True,
        text=True,
    )
    if pid_proc.returncode != 0:
        return False

    # `git patch-id` prints lines of the form: "<patch_id> <commit_id>"
    branch_pids = set()
    for line in pid_proc.stdout.splitlines():
        parts = line.split()
        if parts:
            branch_pids.add(parts[0])

    return target_pid in branch_pids


def _patch_id_of(commit):
    """Return the patch-id (content hash) of a single commit, or None on failure."""
    show = subprocess.run(
        ["git", "show", commit],
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        return None
    pid = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=show.stdout,
        capture_output=True,
        text=True,
    )
    if pid.returncode != 0 or not pid.stdout.strip():
        return None
    return pid.stdout.split()[0]


# --- Step 3: Pre-Flight Checks ---
# TODO: Add FIPS boundary detection here later


# --- Step 4: Backport Engine ---


def cherry_pick_to_branch(commit, branch):
    """
    Attempt to cherry-pick `commit` onto `branch`.
    Returns ("success", new_branch_name) or ("conflict", None).
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
        # Conflict. Abort, switch back, delete the half-built branch.
        subprocess.run(
            ["git", "cherry-pick", "--abort"], capture_output=True, text=True
        )
        subprocess.run(
            ["git", "checkout", original_branch], capture_output=True, text=True
        )
        subprocess.run(
            ["git", "branch", "-D", new_branch], capture_output=True, text=True
        )
        return ("conflict", None)

    # Anything else: best-effort cleanup, then raise.
    subprocess.run(["git", "cherry-pick", "--abort"], capture_output=True, text=True)
    subprocess.run(["git", "checkout", original_branch], capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", new_branch], capture_output=True, text=True)
    raise RuntimeError(f"cherry-pick failed: {pick.stderr}")


# --- Step 5: PR Creation & Summary ---


def open_pr(pr_branch, target_branch, commit, pr_number):
    """
    Open a pull request from the backport branch to the target branch.
    - Uses the `gh` CLI under the hood.
    - Idempotent: if a PR for this head/base pair already exists, returns
      its URL instead of failing.
    - Returns the URL of the (new or existing) PR.
    """
    # 1. Check whether an open PR for this head/base pair already exists.
    existing = subprocess.run(
        [
            "gh",
            "pr",
            "list",
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


def post_summary(pr_number, results):
    """
    Post a summary comment on the original PR listing what happened on each branch.
    `results` is a list of (branch, status, url_or_none) tuples.
    """
    marker = {
        "success": "[OK]",
        "conflict": "[!!]",
        "not_affected": "[>>]",
        "already_patched": "[==]",
    }

    lines = [f"**Backport summary for #{pr_number}**", ""]
    for branch, status, url in results:
        symbol = marker.get(status, "?")
        if status == "success":
            lines.append(f"- {symbol} `{branch}` \u2014 {url}")
        elif status == "conflict":
            lines.append(
                f"- {symbol} `{branch}` \u2014 conflict, manual backport needed"
            )
        elif status == "not_affected":
            lines.append(f"- {symbol} `{branch}` \u2014 not affected")
        elif status == "already_patched":
            lines.append(
                f"- {symbol} `{branch}` \u2014 fix already applied (matching patch-id)"
            )
        else:
            lines.append(f"- {symbol} `{branch}` \u2014 {status}")

    body = "\n".join(lines)

    result = subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--body", body],
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
        if not is_branch_affected(introducers, branch):
            results.append((branch, "not_affected", None))
            continue
        # Even if the branch contains the introducing commit, the fix may have
        # already been applied via a manual cherry-pick (different SHA, same
        # content). Skip those branches — a backport would be redundant.
        if is_already_patched(commit, branch):
            results.append((branch, "already_patched", None))
            continue
        status, new_branch = cherry_pick_to_branch(commit, branch)
        if status == "success":
            url = open_pr(new_branch, branch, commit, pr_number)
            results.append((branch, "success", url))
        else:
            results.append((branch, status, None))

    post_summary(pr_number, results)


if __name__ == "__main__":
    main()
