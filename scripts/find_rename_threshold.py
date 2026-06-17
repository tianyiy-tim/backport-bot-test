"""
Empirically locate git's rename-tracking threshold (AWS-LC POC environment).

We rename a file AND rewrite a controlled fraction of it in the SAME commit,
sweeping the amount rewritten from 0% up to 100%. For each step we record:

  * sim     - git's computed similarity score (forced via -M01, always emitted)
  * default - whether default rename detection (-M = 50%) calls it a rename
  * follow  - whether `git log --follow <newpath>` reaches the pre-rename commit

The base file is 100 unique lines, so "keep K lines" gives roughly K% content
overlap and lets us walk right up to the boundary.

Run from the project root:

    python3 scripts/find_rename_threshold.py
"""

import shutil
import subprocess
import tempfile
from pathlib import Path


def git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def rev(cwd, ref="HEAD"):
    return git(cwd, "rev-parse", ref).strip()


def base_file(n=100):
    return "".join(f"int original_line_{i}(void) {{ return {i}; }}\n" for i in range(n))


def variant(keep, n=100):
    """Keep the first `keep` original lines; replace the rest with new content."""
    kept = [f"int original_line_{i}(void) {{ return {i}; }}" for i in range(keep)]
    new = [
        f"long rewritten_symbol_{i} = 0x{i:04x}ULL; /* engine v2 */"
        for i in range(keep, n)
    ]
    return "\n".join(kept + new) + "\n"


def similarity(cwd, commit, newpath):
    """git's R-score for newpath in `commit` at a 1% threshold (always emitted)."""
    out = git(cwd, "diff", "--name-status", "-M01", f"{commit}^", commit)
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[-1] == newpath and parts[0].startswith("R"):
            return int(parts[0][1:])
    return None  # not even a rename at 1% -> essentially 0% similar


def is_rename_default(cwd, commit, newpath):
    out = git(cwd, "diff", "--name-status", "-M", f"{commit}^", commit)
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[-1] == newpath:
            return parts[0].startswith("R")
    return False


def follow_crosses(cwd, newpath, target):
    out = git(cwd, "log", "--follow", "--format=%H", "main", "--", newpath)
    return target in out.split()


def run():
    root = tempfile.mkdtemp(prefix="awslc-threshold-")
    try:
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "poc@aws-lc.test")
        git(root, "config", "user.name", "AWS-LC POC")
        git(root, "config", "diff.renames", "true")

        # Introduce crypto/digest.c (the pre-rename introducer we want to reach).
        Path(root, "crypto").mkdir()
        Path(root, "crypto/digest.c").write_text(base_file())
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "add crypto/digest.c")
        intro = rev(root)

        print(f"Sandbox: {root}")
        print(f"Pre-rename introducer: {intro[:8]}")
        print("Base file: 100 unique lines\n")
        print(
            f"  {'rewrite%':>8} {'keep%':>6} {'sim':>5} {'default -M (50%)':>17} {'log --follow':>14}"
        )
        print(f"  {'-' * 8} {'-' * 6} {'-' * 5} {'-' * 17} {'-' * 14}")

        rows = []
        # Walk from "barely changed" to "almost fully rewritten".
        for keep in range(100, -1, -5):
            git(root, "checkout", "-q", "-B", f"probe_{keep}", intro)
            git(root, "mv", "crypto/digest.c", "crypto/hash_engine.c")
            Path(root, "crypto/hash_engine.c").write_text(variant(keep))
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", f"rename+rewrite keep={keep}")
            commit = rev(root)
            git(root, "branch", "-q", "-f", "main", commit)
            git(root, "checkout", "-q", "main")

            sim = similarity(root, commit, "crypto/hash_engine.c")
            is_ren = is_rename_default(root, commit, "crypto/hash_engine.c")
            crosses = follow_crosses(root, "crypto/hash_engine.c", intro)
            rows.append((100 - keep, keep, sim, is_ren, crosses))

            sim_s = f"{sim}%" if sim is not None else "<1%"
            print(
                f"  {100 - keep:>7}% {keep:>5}% {sim_s:>5} "
                f"{str(is_ren):>17} {str(crosses):>14}"
            )

        # Find the boundary for `git log --follow`.
        tracked = [r for r in rows if r[4]]
        lost = [r for r in rows if not r[4]]
        print("\n" + "=" * 60)
        print("Threshold")
        print("=" * 60)
        if tracked and lost:
            worst_tracked = min(tracked, key=lambda r: r[2] if r[2] is not None else 0)
            best_lost = max(lost, key=lambda r: r[2] if r[2] is not None else 0)
            print(
                f"  Last similarity that STILL tracks:  {worst_tracked[2]}% "
                f"(rewrote {worst_tracked[0]}%)"
            )
            print(
                f"  First similarity that LOSES tracking: "
                f"{best_lost[2] if best_lost[2] is not None else '<1'}% "
                f"(rewrote {best_lost[0]}%)"
            )
            print(
                "\n  => git keeps tracking the rename while similarity >= 50%, "
                "and drops it below 50%.\n     This is git's built-in default "
                "rename threshold (-M50%)."
            )
        else:
            print("  Could not bracket the boundary in this sweep.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    run()
