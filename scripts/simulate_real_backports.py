"""
Simulation of the real AWS-LC manual backports that motivated this bot.

WHAT THIS REPRODUCES
--------------------
Three real situations the AWS-LC team handled by hand, the last two of which
arrived at the same time and were painful to juggle (which release/branch is
impacted by which issue, and remembering to cut every cherry-pick PR):

  Feature request
      mainline development      aws/aws-lc#3270
      [CHERRYPICK FIPS 4.0]     aws/aws-lc#3272
      [CHERRYPICK FIPS 3.x]     aws/aws-lc#3273

  Security Issue 1  (P391092217)
      mainline development      aws/aws-lc#3105
      [CHERRYPICK FIPS 4.0]     aws/aws-lc#3109
      [CHERRYPICK FIPS 3.0]     aws/aws-lc#3106
      [FIPS 2.0] not impacted
      [FIPS 1.0] not impacted

  Security Issue 2  (V2128336380)
      mainline development      aws/aws-lc#3107
      [CHERRYPICK FIPS 4.0]     aws/aws-lc#3108
      [CHERRYPICK FIPS 3.0]     aws/aws-lc#3106
      [CHERRYPICK NetOS]        aws/aws-lc#3110  (one-off team branch)
      [FIPS 2.0] not impacted
      [FIPS 1.0] not impacted

HOW IT WORKS
------------
This builds an isolated sandbox git repo whose branch fork-points reproduce the
documented impact (Section below), mirrors every branch to refs/remotes/origin/*
so the bot sees them exactly as it would on GitHub, then runs the REAL
deterministic engine from backport_bot.py against each scenario's fix commit:

    get_changed_files -> find_introducing_commit -> is_branch_affected
    -> is_already_patched -> cherry-pick simulation

It prints a per-scenario verdict table, a combined "two issues at once"
dashboard (the actual pain point), and asserts the engine's verdict matches the
documented manual outcome for every (branch x scenario) cell.

The engine is deterministic, so this needs NO AWS credentials. The AI advisory
layer only ever activates when ancestry + patch-id are both inconclusive; here
every cell resolves deterministically, which is the point: the cases the team
agonized over are the easy, mechanical ones for git.

WHY THE FORK-POINTS LOOK THIS WAY
---------------------------------
Linear mainline history, oldest first:

    C0  init (app.c, utils/buffer.c)                  <- FIPS-1.0, FIPS-2.0 fork
    C1  add tls/session.c        [Issue 2 lives here] <- NetOS forks
    C2  add crypto/aead.c        [Issue 1 lives here]
    C3  add crypto/kdf.c         [Feature lives here] <- FIPS-3.0, FIPS-4.0 fork
    ... fix commits (the mainline PRs) land on main on top of C3

So:
  - FIPS-1.0 / FIPS-2.0 forked before any of the three components existed
    -> not impacted by anything (matches "[FIPS 2.0]/[FIPS 1.0] not impacted").
  - NetOS forked right after tls/session.c was added but before aead.c / kdf.c
    -> carries Issue 2's code only (matches NetOS impacted by Issue 2 #3110,
       and not listed for Issue 1 or the feature).
  - FIPS-3.0 / FIPS-4.0 forked after all three components existed
    -> impacted by all three (matches every 3.x / 4.0 cherry-pick PR above).

Run from the project root:

    python3 scripts/simulate_real_backports.py

Set KEEP_SANDBOX=1 to leave the throwaway repo on disk for inspection.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backport_bot import (  # noqa: E402
    find_introducing_commit,
    get_changed_files,
    get_supported_branches,
    is_already_patched,
    is_branch_affected,
)

# Branches in display order (oldest release -> newest, then the one-off, then main).
RELEASE_BRANCHES = [
    "AWS-LC-FIPS-1.0",
    "AWS-LC-FIPS-2.0",
    "AWS-LC-FIPS-3.0",
    "AWS-LC-FIPS-4.0",
    "NetOS",
]


# ===========================================================================
# Source fixtures (mirror the AWS-LC component layout: crypto/*, tls/*, utils/*)
# ===========================================================================

APP_C = "int main() { return 0; }\n"

BUFFER_C = """\
#include <string.h>

void copy_buffer(char *dst, const char *src, int len) {
    memcpy(dst, src, len);
}
"""

# Issue 2 component (tls/session.c) -- the OLDEST of the three, so NetOS can
# carry it without carrying Issue 1 or the feature.
SESSION_C = """\
#include <string.h>

#define MAX_SESSION 256

void resume_session(char *out, const char *ticket, int ticket_len) {
    memcpy(out, ticket, ticket_len);
}

int session_id_valid(const char *id) {
    return 1;
}
"""

# Issue 1 component (crypto/aead.c).
AEAD_C = """\
#include <string.h>

#define MAX_TAG 16

int aead_open(char *out, const char *ct, int ct_len, const char *tag) {
    memcpy(out, ct, ct_len);
    return 1;
}
"""

# Feature component (crypto/kdf.c) -- the feature request enhances this.
KDF_C = """\
#include <string.h>

#define KDF_OUT 32

int derive_key(char *out, const char *secret, int secret_len) {
    memcpy(out, secret, KDF_OUT);
    return 1;
}
"""


# ===========================================================================
# Scenario definitions (fix applied on main + documented manual outcome)
# ===========================================================================


class Scenario:
    """
    One real backport situation.

    fix(root) applies the mainline fix/feature edit on main and returns nothing;
    the harness records the resulting commit SHA as the bot's input.

    ground_truth is the documented manual outcome: the set of release branches
    that were actually patched (the cherry-pick PRs that were cut). Everything
    else is "not impacted".
    """

    def __init__(self, key, title, ticket, mainline_pr, fix, ground_truth, pr_refs):
        self.key = key
        self.title = title
        self.ticket = ticket
        self.mainline_pr = mainline_pr
        self.fix = fix
        self.ground_truth = ground_truth
        self.pr_refs = pr_refs  # branch -> real PR ref, for traceability
        self.commit = None
        self.files = None
        self.introducers = None
        self.verdicts = {}  # branch -> dict(verdict, reason, cherry_pick)


def feature_fix(root):
    # Enhance an existing kdf.c function (introduced at C3): add a length guard.
    _replace(
        root,
        "crypto/kdf.c",
        "int derive_key(char *out, const char *secret, int secret_len) {\n"
        "    memcpy(out, secret, KDF_OUT);",
        "int derive_key(char *out, const char *secret, int secret_len) {\n"
        "    if (secret_len < KDF_OUT) {\n"
        "        return 0;\n"
        "    }\n"
        "    memcpy(out, secret, KDF_OUT);",
    )


def issue1_fix(root):
    # Security Issue 1: bound the AEAD open against the tag length (crypto/aead.c).
    _replace(
        root,
        "crypto/aead.c",
        "int aead_open(char *out, const char *ct, int ct_len, const char *tag) {\n"
        "    memcpy(out, ct, ct_len);",
        "int aead_open(char *out, const char *ct, int ct_len, const char *tag) {\n"
        "    if (ct_len < MAX_TAG) {\n"
        "        return 0;\n"
        "    }\n"
        "    memcpy(out, ct, ct_len);",
    )


def issue2_fix(root):
    # Security Issue 2: bound the session ticket copy (tls/session.c).
    _replace(
        root,
        "tls/session.c",
        "void resume_session(char *out, const char *ticket, int ticket_len) {\n"
        "    memcpy(out, ticket, ticket_len);",
        "void resume_session(char *out, const char *ticket, int ticket_len) {\n"
        "    if (ticket_len > MAX_SESSION) {\n"
        "        return;\n"
        "    }\n"
        "    memcpy(out, ticket, ticket_len);",
    )


SCENARIOS = [
    Scenario(
        key="feature-request",
        title="Feature request",
        ticket=None,
        mainline_pr="aws/aws-lc#3270",
        fix=feature_fix,
        ground_truth={"AWS-LC-FIPS-3.0", "AWS-LC-FIPS-4.0"},
        pr_refs={
            "AWS-LC-FIPS-4.0": "aws/aws-lc#3272",
            "AWS-LC-FIPS-3.0": "aws/aws-lc#3273",
        },
    ),
    Scenario(
        key="security-issue-1",
        title="Security Issue 1",
        ticket="P391092217",
        mainline_pr="aws/aws-lc#3105",
        fix=issue1_fix,
        ground_truth={"AWS-LC-FIPS-3.0", "AWS-LC-FIPS-4.0"},
        pr_refs={
            "AWS-LC-FIPS-4.0": "aws/aws-lc#3109",
            "AWS-LC-FIPS-3.0": "aws/aws-lc#3106",
        },
    ),
    Scenario(
        key="security-issue-2",
        title="Security Issue 2",
        ticket="V2128336380",
        mainline_pr="aws/aws-lc#3107",
        fix=issue2_fix,
        ground_truth={"AWS-LC-FIPS-3.0", "AWS-LC-FIPS-4.0", "NetOS"},
        pr_refs={
            "AWS-LC-FIPS-4.0": "aws/aws-lc#3108",
            "AWS-LC-FIPS-3.0": "aws/aws-lc#3106",
            "NetOS": "aws/aws-lc#3110",
        },
    ),
]


# ===========================================================================
# Git helpers
# ===========================================================================


def run(args, cwd, check=True):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(args)}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def git(cwd, *args, check=True):
    return run(["git", *args], cwd, check=check)


def rev(cwd, ref="HEAD"):
    return git(cwd, "rev-parse", ref).stdout.strip()


def write(root, relpath, content):
    path = Path(root) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _replace(root, relpath, old, new):
    full = Path(root) / relpath
    text = full.read_text()
    assert old in text, f"anchor not found in {relpath}"
    full.write_text(text.replace(old, new, 1))


@contextmanager
def chdir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


# ===========================================================================
# Fixture construction
# ===========================================================================


def build_fixture(root):
    """Build the synthetic AWS-LC history + branches, mirrored to origin/*."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "poc@aws-lc.test")
    git(root, "config", "user.name", "AWS-LC POC")
    git(root, "config", "diff.renames", "true")

    # C0: initial commit. FIPS-1.0 and FIPS-2.0 fork here (before any component).
    write(root, "app.c", APP_C)
    write(root, "utils/buffer.c", BUFFER_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial commit (app.c, utils/buffer.c)")
    c0 = rev(root)

    # C1: add tls/session.c (Issue 2 component). NetOS forks here.
    write(root, "tls/session.c", SESSION_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add tls/session.c (session resumption)")
    c1 = rev(root)

    # C2: add crypto/aead.c (Issue 1 component).
    write(root, "crypto/aead.c", AEAD_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add crypto/aead.c (AEAD open/seal)")

    # C3: add crypto/kdf.c (feature component). FIPS-3.0 / FIPS-4.0 fork here.
    write(root, "crypto/kdf.c", KDF_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add crypto/kdf.c (key derivation)")
    c3 = rev(root)

    # Place release branches at their fork points.
    git(root, "branch", "AWS-LC-FIPS-1.0", c0)
    git(root, "branch", "AWS-LC-FIPS-2.0", c0)
    git(root, "branch", "AWS-LC-FIPS-3.0", c3)
    git(root, "branch", "AWS-LC-FIPS-4.0", c3)

    # NetOS: one-off branch forked at C1 (has session.c only), plus a custom
    # commit that touches app.c -- divergent history, but not the security code,
    # so the Issue 2 cherry-pick still applies cleanly (as it did via #3110).
    git(root, "checkout", "-q", "-b", "NetOS", c1)
    write(
        root, "app.c", '#include <stdio.h>\nint main() { puts("NetOS"); return 0; }\n'
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "NetOS: custom entrypoint for one-off team build")
    git(root, "checkout", "-q", "main")

    # Mirror every branch to refs/remotes/origin/* so the bot resolves them via
    # `git branch -r` and `origin/<branch>` exactly as it would against GitHub.
    for b in ["main", *RELEASE_BRANCHES]:
        git(root, "update-ref", f"refs/remotes/origin/{b}", b)


def apply_fix_on_main(root, scenario):
    """Apply a scenario's mainline fix as a fresh commit on main, refresh origin/main."""
    git(root, "checkout", "-q", "main")
    scenario.fix(root)
    git(root, "add", "-A")
    label = scenario.ticket or "feature"
    git(
        root,
        "commit",
        "-q",
        "-m",
        f"fix: {scenario.title} ({label}) [{scenario.mainline_pr}]",
    )
    sha = rev(root)
    git(root, "update-ref", "refs/remotes/origin/main", "main")
    return sha


def simulate_cherry_pick(root, commit, branch):
    """Attempt a cherry-pick onto origin/<branch> in a throwaway worktree."""
    parent = tempfile.mkdtemp(prefix="sim-cp-")
    worktree = os.path.join(parent, "wt")
    try:
        add = git(
            root,
            "worktree",
            "add",
            "--detach",
            worktree,
            f"origin/{branch}",
            check=False,
        )
        if add.returncode != 0:
            return "error"
        pick = git(worktree, "cherry-pick", commit, check=False)
        if pick.returncode == 0:
            return "clean"
        combined = (pick.stdout + pick.stderr).lower()
        if "empty" in combined or "nothing to commit" in combined:
            return "empty"
        return "conflict"
    finally:
        git(worktree, "cherry-pick", "--abort", check=False)
        git(root, "worktree", "remove", "--force", worktree, check=False)
        shutil.rmtree(parent, ignore_errors=True)
        git(root, "worktree", "prune", check=False)


# ===========================================================================
# Driver
# ===========================================================================


def run_scenario(root, scenario):
    """Run the real deterministic engine against one scenario's fix commit."""
    commit = apply_fix_on_main(root, scenario)
    scenario.commit = commit
    scenario.files = get_changed_files(commit)
    scenario.introducers = find_introducing_commit(commit, scenario.files)

    for branch in RELEASE_BRANCHES:
        affected, advisory = is_branch_affected(
            scenario.introducers, branch, commit=commit, changed_files=scenario.files
        )
        if not affected:
            scenario.verdicts[branch] = {
                "verdict": False,
                "reason": "not_affected" if advisory is None else "not_affected_ai",
                "cherry_pick": None,
            }
            continue
        if is_already_patched(commit, branch):
            scenario.verdicts[branch] = {
                "verdict": False,
                "reason": "already_patched",
                "cherry_pick": None,
            }
            continue
        cp = simulate_cherry_pick(root, commit, branch)
        scenario.verdicts[branch] = {
            "verdict": True,
            "reason": "needs_backport",
            "cherry_pick": cp,
        }


def classify(bot_affected, truth_affected):
    if bot_affected and truth_affected:
        return "TP"
    if bot_affected and not truth_affected:
        return "FP"
    if not bot_affected and truth_affected:
        return "FN"
    return "TN"


def print_scenario(scenario):
    print(f"\n{'=' * 92}")
    header = scenario.title
    if scenario.ticket:
        header += f"  ({scenario.ticket})"
    print(header)
    print(f"  mainline fix: {scenario.mainline_pr}   commit {scenario.commit[:10]}")
    print(f"{'=' * 92}")
    print(f"  changed files: {scenario.files}")
    print(f"  introducer(s): {sorted(s[:8] for s in scenario.introducers)}")
    print()
    print(
        f"  {'branch':<18} {'bot verdict':<14} {'reason':<16} {'cherry-pick':<12} "
        f"{'manual outcome':<16} {'real PR':<16} {'match'}"
    )
    print(
        f"  {'-' * 18} {'-' * 14} {'-' * 16} {'-' * 12} {'-' * 16} {'-' * 16} {'-' * 5}"
    )

    labels = []
    for branch in RELEASE_BRANCHES:
        data = scenario.verdicts[branch]
        truth = branch in scenario.ground_truth
        label = classify(data["verdict"], truth)
        labels.append(label)

        bot_str = "BACKPORT" if data["verdict"] else "skip"
        cp = data["cherry_pick"] or ""
        manual = "patched" if truth else "not impacted"
        pr = scenario.pr_refs.get(branch, "")
        ok = "OK" if label in ("TP", "TN") else f"{label}!"
        print(
            f"  {branch:<18} {bot_str:<14} {data['reason']:<16} {cp:<12} "
            f"{manual:<16} {pr:<16} {ok}"
        )
    return labels


def print_dashboard():
    """The actual pain point: both security issues side by side, in one view."""
    issues = [s for s in SCENARIOS if s.ticket]
    print(f"\n{'=' * 92}")
    print("Combined dashboard - both security issues at once (the original pain point)")
    print(f"{'=' * 92}")
    head = f"  {'branch':<18}"
    for s in issues:
        head += f" {s.title + ' (' + s.ticket + ')':<34}"
    print(head)
    print(f"  {'-' * 18}" + "".join(f" {'-' * 34}" for _ in issues))
    for branch in RELEASE_BRANCHES:
        row = f"  {branch:<18}"
        for s in issues:
            data = s.verdicts[branch]
            if data["verdict"]:
                cell = f"BACKPORT  -> {s.pr_refs.get(branch, '?')}"
            else:
                cell = "not impacted"
            row += f" {cell:<34}"
        print(row)


def main():
    keep = os.environ.get("KEEP_SANDBOX") == "1"
    root = tempfile.mkdtemp(prefix="awslc-real-backports-")
    print(f"Sandbox: {root}")
    print(
        "Reproduces the real aws/aws-lc manual backports (feature + 2 security issues)."
    )

    try:
        build_fixture(root)

        # Sanity: the bot's own branch resolver should discover our branches.
        with chdir(root):
            discovered = set(get_supported_branches())
        missing = set(RELEASE_BRANCHES) - discovered
        if missing:
            print(
                f"\n[FAIL] branch resolver missed: {sorted(missing)}", file=sys.stderr
            )
            return 1

        with chdir(root):
            for scenario in SCENARIOS:
                run_scenario(root, scenario)

        all_labels = []
        for scenario in SCENARIOS:
            all_labels.extend(print_scenario(scenario))

        print_dashboard()

        # ----- Scorecard -----
        summary = {"TP": 0, "TN": 0, "FP": 0, "FN": 0}
        for lbl in all_labels:
            summary[lbl] += 1
        total = sum(summary.values())
        correct = summary["TP"] + summary["TN"]

        print(f"\n{'=' * 92}")
        print("Scorecard - engine verdict vs. documented manual outcome")
        print(f"{'=' * 92}")
        print(f"  scenarios:                 {len(SCENARIOS)}")
        print(f"  (branch x scenario) cells: {total}")
        print(f"  true positives  (correctly backported): {summary['TP']}")
        print(f"  true negatives  (correctly skipped):     {summary['TN']}")
        print(f"  false positives (unnecessary PRs):       {summary['FP']}")
        print(f"  false negatives (MISSED backports):      {summary['FN']}")
        print(
            f"  agreement with manual outcome: {correct}/{total} ({correct / total * 100:.0f}%)"
        )

        cps = [
            v["cherry_pick"]
            for s in SCENARIOS
            for v in s.verdicts.values()
            if v["cherry_pick"]
        ]
        print(
            f"  cherry-picks attempted: {len(cps)} "
            f"(clean={cps.count('clean')}, conflict={cps.count('conflict')}, "
            f"empty={cps.count('empty')}, error={cps.count('error')})"
        )

        ok = summary["FP"] == 0 and summary["FN"] == 0
        print()
        if ok:
            print(
                "RESULT: PASS - the engine reproduces every documented backport decision."
            )
        else:
            print("RESULT: FAIL - engine disagreed with the documented manual outcome.")
        return 0 if ok else 1

    finally:
        if keep:
            print(f"\nKEEP_SANDBOX=1 -> leaving sandbox at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
