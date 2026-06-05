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

# TODO: Add FIPS boundary detection later
# FIPS_BOUNDARY_PATHS = ["crypto/fipsmodule/"]


# --- Step 1: Branch Resolver ---


def get_supported_branches():
    """
    Get list of currently supported branches.
    - Query remote branches matching naming conventions (e.g., AWS-LC-FIPS-*)
    - Filter by end-of-support dates from VERSIONING.md  (skip for now)
    - Return list of branch names (without the "origin/" prefix)
    """
    # TODO: implement
    #   1. Run `git branch -r` via subprocess.run(...)
    #   2. Read the captured stdout
    #   3. Split it into lines
    #   4. For each line: strip whitespace, skip the "HEAD -> ..." line,
    #      keep only lines that start with "origin/AWS-LC-FIPS-"
    #   5. Strip the "origin/" prefix from each kept name
    #   6. Return the list

    result = subprocess.run(["git", "branch", "-r"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git branch -r failed: {result.stderr}")

    branches = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("origin/AWS-LC-FIPS-"):
            continue
        line = line[len("origin/") :]
        branches.append(line)
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
    - Use git blame on the parent of the fix commit
    - Focus on the lines that were removed/changed by the fix
    - Return set of introducing commit hashes
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

        # 2. Walk each hunk header (@@ -A,B +C,D @@) and decide which lines to blame.
        for line in result.stdout.splitlines():
            if not line.startswith("@@"):
                continue
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? ", line)
            if not match:
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) else 1

            if old_count == 0:
                # Pure addition: blame the line right after the insertion point.
                blame_start = old_start + 1
                blame_end = old_start + 1
            else:
                # Lines were removed/modified: blame those exact lines.
                blame_start = old_start
                blame_end = old_start + old_count - 1

            # 3. Run blame on the parent for that line range.
            line_range = f"{blame_start},{blame_end}"
            blame_result = subprocess.run(
                ["git", "blame", "-L", line_range, f"{commit}^", "--", file],
                capture_output=True,
                text=True,
            )
            if blame_result.returncode != 0:
                raise RuntimeError(f"git blame failed: {blame_result.stderr}")

            # 4. The first whitespace-separated token of each blame line is the SHA.
            for blame_line in blame_result.stdout.splitlines():
                if not blame_line:
                    continue
                sha = blame_line.split()[0].lstrip("^")
                introducing.add(sha)

    return introducing


def is_branch_affected(introducing_commits, branch):
    """
    Check if the branch contains the vulnerable code.
    - Use git merge-base --is-ancestor to check if any introducing
      commit is in the branch's history
    - Return True if affected, False otherwise
    """
    for sha in introducing_commits:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, branch],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            return True
        if result.returncode != 1:
            raise RuntimeError(f"git merge-base failed (code {result.returncode})")

    return False


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
        "success": "O",
        "conflict": "!",
        "not_affected": ">>",
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
        status, new_branch = cherry_pick_to_branch(commit, branch)
        if status == "success":
            url = open_pr(new_branch, branch, commit, pr_number)
            results.append((branch, "success", url))
        else:
            results.append((branch, status, None))

    post_summary(pr_number, results)


if __name__ == "__main__":
    main()
