"""
backport - local, patch-driven CLI for the AWS-LC backport bot.

Runs the same impact engine as scripts/backport_bot.py, but locally and from a
PATCH rather than a merged commit, so an embargoed security fix can be assessed
(and backported to local branches) before any public code change.

Subcommands:

  analyze <patch>     Materialize the patch on top of a base ref, then bucket
                      every supported branch into AFFECTED / NOT AFFECTED /
                      UNSURE / ALREADY PATCHED. Deterministic only (no AI, no
                      network), and saves the run so the next commands reuse it.

  explain [branch..]  Run the AI advisory on the UNSURE branches (or the ones
                      you name) and print the model's verdict, confidence, and
                      reasoning. Advisory only. This is the only command that
                      sends code to Bedrock, and only when you ask for it.

  apply [--all-affected | --branches ..]
                      Cherry-pick the patch onto the chosen branches in LOCAL
                      branches (backport/<branch>/<id>), reporting clean vs
                      conflict. Never pushes, opens a PR, or auto-merges.

Typical flow:

  git diff > fix.patch                 # the mainline fix, uncommitted is fine
  ./backport analyze fix.patch
  ./backport explain                   # optional: AI justification for unsure
  ./backport apply --all-affected      # local backport branches for review

Run from anywhere inside the AWS-LC clone. The clone must have the release
branches fetched (origin/AWS-LC-FIPS-*, origin/NetOS, origin/main).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

# Reuse the production engine.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
import backport_bot as bot  # noqa: E402

_RUN_DIR = _HERE / ".runs"
_RUN_FILE = _RUN_DIR / "last-run.json"


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def _run(args, check=True, cwd=None):
    p = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(args)}\nstdout: {p.stdout}\nstderr: {p.stderr}"
        )
    return p


def _git(*args, check=True, cwd=None):
    return _run(["git", *args], check=check, cwd=cwd)


def _ref_exists(ref):
    return _git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


# --------------------------------------------------------------------------
# patch materialization
# --------------------------------------------------------------------------


def _looks_like_mbox(patch_text):
    # `git format-patch` output is an mbox; it starts with a "From <sha> <date>" line.
    head = patch_text.lstrip().splitlines()[:1]
    return bool(head) and head[0].startswith("From ")


def _derive_message(patch_text, fallback="Backport candidate (from patch)"):
    for line in patch_text.splitlines():
        if line.startswith("Subject:"):
            return (
                line[len("Subject:") :].strip().lstrip("[PATCH] ").strip() or fallback
            )
    return fallback


@contextmanager
def materialized_fix(patch_text, base, three_way=False):
    """
    Apply *patch_text* on top of *base* in a throwaway worktree, commit it, and
    yield the resulting fix commit SHA. The worktree (and the patch file) are
    torn down on exit; the commit object stays in the shared object store for
    the life of the process, which is all the engine needs.
    """
    if not _ref_exists(base):
        raise RuntimeError(
            f"base ref '{base}' not found. Fetch the mainline first "
            f"(git fetch origin) or pass --base <ref>."
        )

    parent = tempfile.mkdtemp(prefix="backport-fix-")
    wt = os.path.join(parent, "wt")
    patch_file = os.path.join(parent, "fix.patch")
    Path(patch_file).write_text(patch_text)

    try:
        _git("worktree", "add", "--detach", "--quiet", wt, base)

        committed = False
        if _looks_like_mbox(patch_text):
            am_args = ["am", "--3way"] if three_way else ["am"]
            am = _git(*am_args, patch_file, check=False, cwd=wt)
            if am.returncode == 0:
                committed = True
            else:
                _git("am", "--abort", check=False, cwd=wt)

        if not committed:
            apply_args = ["apply", "--3way"] if three_way else ["apply"]
            ap = _git(*apply_args, patch_file, check=False, cwd=wt)
            if ap.returncode != 0:
                raise RuntimeError(
                    "patch did not apply onto "
                    f"{base}:\n{(ap.stderr or ap.stdout).strip()}\n"
                    "If your local mainline has drifted, retry with --3way."
                )
            _git("add", "-A", cwd=wt)
            _git(
                "-c",
                "user.name=backport-cli",
                "-c",
                "user.email=backport-cli@local",
                "commit",
                "--quiet",
                "-m",
                _derive_message(patch_text),
                cwd=wt,
            )

        sha = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        yield sha
    finally:
        _git("worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(parent, ignore_errors=True)
        _git("worktree", "prune", check=False)


# --------------------------------------------------------------------------
# impact analysis (deterministic bucketing)
# --------------------------------------------------------------------------

AFFECTED = "affected"
NOT_AFFECTED = "not_affected"
UNSURE = "unsure"
ALREADY = "already_patched"

_LABEL = {
    AFFECTED: "AFFECTED",
    NOT_AFFECTED: "not affected",
    UNSURE: "UNSURE",
    ALREADY: "already patched",
}


def _changed_files_with_status(commit):
    """Return (all_paths, introducer_paths). Added files have no prior history."""
    out = _git("diff-tree", "--no-commit-id", "--name-status", "-r", commit).stdout
    all_paths, introducer_paths = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        all_paths.append(path)
        # 'A' = newly added by the fix, so there is no introducing commit to trace.
        if not status.startswith("A"):
            introducer_paths.append(path)
    return all_paths, introducer_paths


def bucket_branches(fix_sha, branches):
    """Classify each branch deterministically (no AI). Returns dict branch->state."""
    files, introducer_files = _changed_files_with_status(fix_sha)
    introducers = bot.find_introducing_commit(fix_sha, introducer_files)

    buckets = {}
    for branch in branches:
        affected, _ = bot.is_branch_affected(introducers, branch)  # Path 1 + Path 2
        if affected:
            buckets[branch] = (
                ALREADY if bot.is_already_patched(fix_sha, branch) else AFFECTED
            )
            continue
        # Not matched by ancestry/patch-id: present-but-unprovable is UNSURE,
        # absent-everywhere is a confident NOT AFFECTED.
        ref = f"origin/{branch}"
        present = any(
            bot._get_file_on_branch(f, ref, commit=fix_sha)[0] is not None
            for f in files
        )
        buckets[branch] = UNSURE if present else NOT_AFFECTED
    return files, sorted(introducers), buckets


# --------------------------------------------------------------------------
# run-state persistence
# --------------------------------------------------------------------------


def _save_run(patch_text, base, branches, buckets):
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _RUN_FILE.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "base": base,
                "branches": branches,
                "buckets": buckets,
                "patch": patch_text,
            },
            indent=2,
        )
    )


def _load_run():
    if not _RUN_FILE.exists():
        raise RuntimeError(
            "no saved run found. Run `backport analyze <patch>` first, "
            "or pass --patch <file>."
        )
    return json.loads(_RUN_FILE.read_text())


def _resolve_patch_and_base(args):
    """Use --patch/--base if given, else fall back to the saved run."""
    if getattr(args, "patch", None):
        patch_text = Path(args.patch).read_text()
        base = args.base or "origin/main"
        return patch_text, base, None
    run = _load_run()
    base = args.base or run.get("base", "origin/main")
    return run["patch"], base, run


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _print_summary(fix_sha, files, introducers, buckets):
    print(f"Fix commit (materialized): {fix_sha[:10]}")
    print(f"Changed files: {files}")
    print(f"Introducer(s): {[s[:8] for s in introducers] or '(none / new file)'}")
    print()
    print(f"  {'branch':<22} {'status':<16}")
    print(f"  {'-' * 22} {'-' * 16}")
    for branch, state in buckets.items():
        print(f"  {branch:<22} {_LABEL[state]:<16}")

    def names(state):
        return [b for b, s in buckets.items() if s == state]

    print()
    aff, uns, no, pat = (
        names(AFFECTED),
        names(UNSURE),
        names(NOT_AFFECTED),
        names(ALREADY),
    )
    print(f"Affected (need backport): {', '.join(aff) or '-'}")
    print(f"Unsure (run `backport explain`): {', '.join(uns) or '-'}")
    print(f"Not affected: {', '.join(no) or '-'}")
    if pat:
        print(f"Already patched (skip): {', '.join(pat)}")


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def cmd_analyze(args):
    patch_text = Path(args.patch).read_text()
    base = args.base or "origin/main"
    branches = args.branches or bot.get_supported_branches()
    if not branches:
        print(
            "No supported branches found. Is this an AWS-LC clone with the "
            "release branches fetched (git fetch origin)?",
            file=sys.stderr,
        )
        return 1

    with materialized_fix(patch_text, base, three_way=args.three_way) as fix_sha:
        files, introducers, buckets = bucket_branches(fix_sha, branches)
        if args.json:
            print(
                json.dumps(
                    {
                        "fix_commit": fix_sha,
                        "base": base,
                        "changed_files": files,
                        "introducers": introducers,
                        "buckets": buckets,
                    },
                    indent=2,
                )
            )
        else:
            _print_summary(fix_sha, files, introducers, buckets)

    _save_run(patch_text, base, branches, buckets)
    return 0


def cmd_explain(args):
    patch_text, base, run = _resolve_patch_and_base(args)
    with materialized_fix(patch_text, base, three_way=args.three_way) as fix_sha:
        files, introducer_files = _changed_files_with_status(fix_sha)
        introducers = bot.find_introducing_commit(fix_sha, introducer_files)
        branches = args.branches or run["branches"]
        buckets = run["buckets"] if run else bucket_branches(fix_sha, branches)[2]

        targets = args.branches or [b for b, s in buckets.items() if s == UNSURE]
        if not targets:
            print(
                "Nothing to explain: no UNSURE branches. (Pass branch names to force.)"
            )
            return 0

        client_probe = bot._ai_client()
        if client_probe is None:
            print(
                "AI advisory unavailable: no Bedrock client (missing SDK or AWS "
                "credentials). Deterministic buckets stand; rerun with creds to "
                "get justifications.",
                file=sys.stderr,
            )
            return 1

        print(f"AI advisory for {len(targets)} branch(es). Advisory only.\n")
        for branch in targets:
            adv = bot.ai_impact_analysis(fix_sha, branch, files, introducers)
            print(f"== {branch} ==")
            if adv is None:
                print("  (no response from the model; see stderr)\n")
                continue
            verdict = {
                True: "likely affected",
                False: "likely NOT affected",
                None: "uncertain",
            }[adv.get("likely_affected")]
            print(f"  verdict:    {verdict}")
            print(f"  confidence: {adv.get('confidence')}")
            print(f"  reasoning:  {adv.get('reasoning', '').strip()}\n")
    return 0


def _cherry_pick_local(fix_sha, branch, run_id):
    """Cherry-pick fix_sha onto origin/<branch> in a throwaway worktree.

    On a clean apply, create a local branch backport/<branch>/<run_id> at the
    result and return ("clean", branch_name). On conflict, abort and return
    ("conflict", first_conflict_line). Never pushes or opens a PR.
    """
    ref = f"origin/{branch}"
    if not _ref_exists(ref):
        return "error", f"{ref} not found"
    parent = tempfile.mkdtemp(prefix="backport-cp-")
    wt = os.path.join(parent, "wt")
    try:
        add = _git("worktree", "add", "--detach", "--quiet", wt, ref, check=False)
        if add.returncode != 0:
            return "error", add.stderr.strip()
        pick = _git("cherry-pick", fix_sha, check=False, cwd=wt)
        if pick.returncode == 0:
            new_sha = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            local_branch = f"backport/{branch}/{run_id}"
            _git("branch", "-f", local_branch, new_sha)
            return "clean", local_branch
        combined = pick.stdout + pick.stderr
        first = next(
            (ln for ln in combined.splitlines() if "conflict" in ln.lower()),
            "conflict",
        )
        return "conflict", first.strip()
    finally:
        _git("cherry-pick", "--abort", check=False, cwd=wt)
        _git("worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(parent, ignore_errors=True)
        _git("worktree", "prune", check=False)


def cmd_apply(args):
    patch_text, base, run = _resolve_patch_and_base(args)
    with materialized_fix(patch_text, base, three_way=args.three_way) as fix_sha:
        branches = run["branches"] if run else bot.get_supported_branches()
        buckets = run["buckets"] if run else bucket_branches(fix_sha, branches)[2]

        if args.branches:
            targets = args.branches
        elif args.all_affected:
            targets = [b for b, s in buckets.items() if s == AFFECTED]
        else:
            print(
                "Specify what to apply: --all-affected, or --branches <name..>.",
                file=sys.stderr,
            )
            return 2

        if not targets:
            print("Nothing to apply (no matching branches).")
            return 0

        print("Will cherry-pick the patch onto local branches (no push, no PR):")
        for b in targets:
            print(f"  - {b}  ->  backport/{b}/{fix_sha[:8]}")
        if not args.yes:
            if not sys.stdin.isatty():
                print("\nRefusing to proceed without --yes in a non-interactive shell.")
                return 3
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 0

        print()
        run_id = fix_sha[:8]
        clean, conflict = [], []
        for branch in targets:
            status, detail = _cherry_pick_local(fix_sha, branch, run_id)
            if status == "clean":
                print(f"  [OK] {branch}  ->  {detail}")
                clean.append(branch)
            elif status == "conflict":
                print(f"  [!!] {branch}  ->  conflict: {detail}")
                conflict.append(branch)
            else:
                print(f"  [??] {branch}  ->  error: {detail}")

    print()
    print(f"Clean: {', '.join(clean) or '-'}")
    print(f"Conflicts (resolve by hand): {', '.join(conflict) or '-'}")
    print(
        "\nNothing was pushed or merged. Inspect `git branch --list 'backport/*'`, "
        "then push and open PRs for human review when ready."
    )
    return 0


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="backport",
        description="Local, patch-driven AWS-LC backport impact analysis + apply.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument(
            "--base", help="base ref to apply the patch on (default origin/main)"
        )
        p.add_argument(
            "--3way",
            dest="three_way",
            action="store_true",
            help="use 3-way apply/am when the base has drifted",
        )

    pa = sub.add_parser(
        "analyze", help="bucket branches as affected/unsure/not affected"
    )
    pa.add_argument("patch", help="path to the fix patch (git diff or format-patch)")
    pa.add_argument("--branches", nargs="+", help="limit to these branches")
    pa.add_argument("--json", action="store_true", help="emit JSON")
    add_common(pa)
    pa.set_defaults(func=cmd_analyze)

    pe = sub.add_parser("explain", help="AI justification for the unsure branches")
    pe.add_argument(
        "branches", nargs="*", help="branches to explain (default: all UNSURE)"
    )
    pe.add_argument("--patch", help="patch file (default: the last analyzed run)")
    add_common(pe)
    pe.set_defaults(func=cmd_explain)

    pp = sub.add_parser("apply", help="cherry-pick the patch onto local branches")
    pp.add_argument("--branches", nargs="+", help="branches to apply to")
    pp.add_argument(
        "--all-affected", action="store_true", help="apply to every AFFECTED branch"
    )
    pp.add_argument("--patch", help="patch file (default: the last analyzed run)")
    pp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    add_common(pp)
    pp.set_defaults(func=cmd_apply)

    args = ap.parse_args(argv)

    # Resolve any user-supplied patch path while we are still in the caller's
    # cwd, then move to the repo toplevel so every git pathspec the engine uses
    # is interpreted relative to the repo root (not this tools/ subdirectory).
    if getattr(args, "patch", None):
        args.patch = str(Path(args.patch).resolve())
    top = _git("rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        print("error: not inside a git repository", file=sys.stderr)
        return 1
    os.chdir(top.stdout.strip())

    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
