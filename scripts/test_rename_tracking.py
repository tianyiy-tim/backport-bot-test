"""
Rename-tracking stress test for the backport bot (AWS-LC POC environment).

QUESTION UNDER TEST
-------------------
When a CVE fix lands on a file that was previously RENAMED, can the bot still
trace the vulnerable lines back to their *true* introducing commit?

The bot's `find_introducing_commit` relies on:

    git log -L<start>,<end>:<file> --reverse <ref>

whose ability to walk *across* a rename depends entirely on git's rename
detection — a content *similarity* heuristic with a default 50% threshold.
If a file is renamed AND rewritten past that threshold in the same commit,
git sees a delete + add (no rename), and the line history silently dead-ends
at the rename commit. For the bot that means a wrong (too-recent) introducer,
which can turn into a missed backport (false negative).

This test builds an isolated sandbox repo that mirrors the AWS-LC layout
(crypto/*.c, tls/*.c, utils/*.c, fips-style release branches) and exercises
THREE rename "merges" of increasing severity, each landed as a PR-style
`--no-ff` merge into main:

  Scenario 1 — PURE RENAME
      `git mv a b`, no content change. Similarity = 100%.
      Expectation: git tracks the rename. Bot finds the true introducer.

  Scenario 2 — RENAME + SUB-THRESHOLD EDIT (same commit)
      Rename + a small edit. Similarity stays above 50%.
      Expectation: git still detects the rename. Bot is correct.

  Scenario 3 — RENAME + >90% REWRITE (same commit)
      Rename + replace almost the whole file in one commit. Similarity falls
      below 50%, so git records delete+add, NOT a rename.
      Expectation: `git log --follow` / `git log -L` lose history at the
      rename. The bot mis-attributes the introducer to the rename commit
      instead of the original — a silent FALSE-NEGATIVE risk.

Each scenario asserts the OBSERVED git/bot behavior against the documented
EXPECTATION, so this doubles as a regression guard and as living
documentation of the bot's known blind spot.

Run from the project root:

    python3 scripts/test_rename_tracking.py

Set KEEP_SANDBOX=1 to leave the throwaway repo on disk for inspection.
"""

import os
import re
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
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# ===========================================================================
# Source fixtures (mirrors the AWS-LC POC layout)
# ===========================================================================

BUFFER_C = """\
#include <string.h>

void copy_buffer(char *dst, const char *src, int len) {
    memcpy(dst, src, len);
}
"""

# --- Scenario 1 target: pure rename ---------------------------------------
HANDSHAKE_C = """\
#include <string.h>

#define MAX_HANDSHAKE 4096

void process_handshake(char *buf, const char *input, int len) {
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
"""

# --- Scenario 2 target: rename + small (sub-threshold) edit ----------------
RECORD_C = """\
#include <string.h>

#define MAX_RECORD 16384

void handle_record(char *out, const char *record, int record_len) {
    memcpy(out, record, record_len);
}

int record_type(const char *record) {
    return record[0];
}

int record_version(const char *record) {
    return record[1];
}
"""

# Same file after rename, with a small null/bounds guard added. The bulk of
# the file is untouched, so similarity stays comfortably above 50%.
RECORD_C_RENAMED = """\
#include <string.h>

#define MAX_RECORD 16384

void handle_record(char *out, const char *record, int record_len) {
    if (out == NULL || record == NULL) {
        return;
    }
    memcpy(out, record, record_len);
}

int record_type(const char *record) {
    return record[0];
}

int record_version(const char *record) {
    return record[1];
}
"""

# --- Scenario 3 target: rename + >90% rewrite ------------------------------
DIGEST_C = """\
#include <string.h>

#define DIGEST_LEN 32

void compute_hash(char *out, const char *input, int len) {
    memcpy(out, input, DIGEST_LEN);
}

int hash_compare(const char *a, const char *b) {
    return memcmp(a, b, DIGEST_LEN);
}

void hash_init(void *ctx) {
    (void)ctx;
}

void hash_update(void *ctx, const char *data, int len) {
    (void)ctx;
    (void)data;
    (void)len;
}

void hash_final(void *ctx, char *out) {
    (void)ctx;
    (void)out;
}
"""

# Renamed AND almost entirely rewritten in the same commit. Only `hash_compare`
# (and the headers) survive from the original digest.c, so git's similarity
# score lands far below the 50% rename threshold.
HASH_ENGINE_C = """\
#include <string.h>

#define DIGEST_LEN 32

typedef struct {
    unsigned long long state[8];
    unsigned long long bitcount;
    unsigned char block[128];
    int block_len;
} HASH_CTX;

int hash_engine_init(HASH_CTX *ctx, int algorithm) {
    if (ctx == NULL) {
        return -1;
    }
    memset(ctx, 0, sizeof(*ctx));
    ctx->state[0] = (unsigned long long)algorithm;
    return 0;
}

int hash_engine_absorb(HASH_CTX *ctx, const unsigned char *data, int n) {
    if (ctx == NULL || data == NULL || n < 0) {
        return -1;
    }
    ctx->bitcount += (unsigned long long)n * 8ULL;
    return 0;
}

int hash_engine_squeeze(HASH_CTX *ctx, unsigned char *out, int n) {
    if (ctx == NULL || out == NULL || n < 0) {
        return -1;
    }
    memset(out, 0, n);
    return 0;
}

int hash_engine_reset(HASH_CTX *ctx) {
    if (ctx == NULL) {
        return -1;
    }
    ctx->block_len = 0;
    return 0;
}

int hash_compare(const char *a, const char *b) {
    return memcmp(a, b, DIGEST_LEN);
}
"""


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


def short(sha):
    return sha[:8] if sha else "-"


def write(root, relpath, content):
    path = Path(root) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@contextmanager
def chdir(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def line_no(root, ref, path, needle):
    """1-based line number of the first line containing `needle` at `ref:path`."""
    content = git(root, "show", f"{ref}:{path}").stdout.splitlines()
    for idx, line in enumerate(content, start=1):
        if needle in line:
            return idx
    raise AssertionError(f"{needle!r} not found in {ref}:{path}")


# ===========================================================================
# Fixture construction
# ===========================================================================


def build_base(root):
    """Initial AWS-LC-like history. Returns the per-scenario introducer SHAs."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "poc@aws-lc.test")
    git(root, "config", "user.name", "AWS-LC POC")
    # Keep rename behavior deterministic regardless of the user's global config.
    git(root, "config", "diff.renames", "true")

    write(root, "app.c", "int main() { return 0; }\n")
    write(root, "utils/buffer.c", BUFFER_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial commit (app.c, utils/buffer.c)")

    write(root, "crypto/handshake.c", HANDSHAKE_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add crypto/handshake.c with process_handshake")
    intro_s1 = rev(root)

    write(root, "tls/record.c", RECORD_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add tls/record.c with handle_record")
    intro_s2 = rev(root)

    write(root, "crypto/digest.c", DIGEST_C)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "add crypto/digest.c with compute_hash")
    intro_s3 = rev(root)

    # A couple of fips-style release branches forked at this point, so the repo
    # resembles the real fixture (the rename scenarios all play out on main).
    git(root, "branch", "AWS-LC-FIPS-2023", intro_s2)
    git(root, "branch", "AWS-LC-FIPS-2024", intro_s3)

    return intro_s1, intro_s2, intro_s3


def merge_feature(root, branch, mutate, commit_msg, pr_num):
    """
    PR-style flow: branch off main, mutate, commit, then `--no-ff` merge back.
    Returns (feature_commit_sha, merge_commit_sha).
    """
    git(root, "checkout", "-q", "-b", branch, "main")
    mutate(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", commit_msg)
    feat = rev(root)
    git(root, "checkout", "-q", "main")
    git(
        root,
        "merge",
        "-q",
        "--no-ff",
        branch,
        "-m",
        f"Merge pull request #{pr_num} from poc/{branch}",
    )
    merge = rev(root)
    return feat, merge


def cve_fix(root, path, old, new, commit_msg):
    """Apply a small CVE-style fix to `path` on main. Returns the fix SHA."""
    full = Path(root) / path
    text = full.read_text()
    assert old in text, f"anchor {old!r} not in {path}"
    full.write_text(text.replace(old, new, 1))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", commit_msg)
    return rev(root)


# ===========================================================================
# Git rename-tracking probes
# ===========================================================================


def rename_score(root, commit, newpath):
    """
    Inspect how git classifies the change to `newpath` in `commit`, both at the
    default 50% threshold and at a forced 1% threshold (which always yields a
    similarity score). Returns (default_status, similarity_pct_or_None).
    """
    default = git(root, "diff", "--name-status", "-M", f"{commit}^", commit).stdout
    forced = git(root, "diff", "--name-status", "-M01", f"{commit}^", commit).stdout

    def status_for(blob):
        for line in blob.splitlines():
            parts = line.split("\t")
            if parts[-1] == newpath:
                return parts[0]
        return None

    default_status = status_for(default) or "?"
    forced_status = status_for(forced) or ""
    pct = None
    m = re.match(r"R(\d+)", forced_status)
    if m:
        pct = int(m.group(1))
    return default_status, pct


def follow_crosses(root, newpath, target_sha, threshold=None):
    """True if `git log --follow newpath` reaches `target_sha` (pre-rename)."""
    args = ["log", "--follow", "--format=%H"]
    if threshold:
        args.append(f"-M{threshold}")
    args += ["main", "--", newpath]
    shas = git(root, *args).stdout.split()
    return target_sha in shas


def log_l_origin(root, newpath, line):
    """First (oldest) SHA reported by `git log -L line,line:newpath --reverse`."""
    out = git(
        root,
        "log",
        f"-L{line},{line}:{newpath}",
        "--format=%H",
        "--reverse",
        "main",
    ).stdout
    for token in out.splitlines():
        token = token.strip()
        if SHA_RE.match(token):
            return token
    return None


# ===========================================================================
# Test driver
# ===========================================================================


class Scenario:
    def __init__(self, name, newpath, anchor_line, true_intro, rename_commit):
        self.name = name
        self.newpath = newpath
        self.anchor_line = anchor_line  # substring of a line present pre-rename
        self.true_intro = true_intro
        self.rename_commit = rename_commit
        self.results = {}
        self.checks = []  # (label, ok, detail)

    def check(self, label, ok, detail=""):
        self.checks.append((label, ok, detail))

    @property
    def passed(self):
        return all(ok for _, ok, _ in self.checks)


def run_all():
    keep = os.environ.get("KEEP_SANDBOX") == "1"
    root = tempfile.mkdtemp(prefix="awslc-rename-")
    print(f"Sandbox: {root}")
    print("Mirrors the AWS-LC POC layout (crypto/*, tls/*, utils/*, fips branches).\n")

    try:
        return _run_all(root)
    finally:
        if keep:
            print(f"\nKEEP_SANDBOX=1 -> leaving sandbox at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


def _run_all(root):
    intro_s1, intro_s2, intro_s3 = build_base(root)

    # ----- Scenario 1: pure rename ----------------------------------------
    def mv_pure(r):
        git(r, "mv", "crypto/handshake.c", "crypto/tls_handshake.c")

    feat1, _merge1 = merge_feature(
        root,
        "rename/handshake-pure",
        mv_pure,
        "refactor: rename crypto/handshake.c -> crypto/tls_handshake.c",
        101,
    )

    # ----- Scenario 2: rename + sub-threshold edit ------------------------
    def mv_small_edit(r):
        git(r, "mv", "tls/record.c", "tls/tls_record.c")
        write(r, "tls/tls_record.c", RECORD_C_RENAMED)

    feat2, _merge2 = merge_feature(
        root,
        "rename/record-guarded",
        mv_small_edit,
        "refactor: rename tls/record.c -> tls/tls_record.c (+ null guard)",
        102,
    )

    # ----- Scenario 3: rename + >90% rewrite ------------------------------
    def mv_rewrite(r):
        git(r, "mv", "crypto/digest.c", "crypto/hash_engine.c")
        write(r, "crypto/hash_engine.c", HASH_ENGINE_C)

    feat3, _merge3 = merge_feature(
        root,
        "rename/digest-rewrite",
        mv_rewrite,
        "refactor: rewrite + rename crypto/digest.c -> crypto/hash_engine.c",
        103,
    )

    scenarios = [
        Scenario(
            "S1 pure rename",
            "crypto/tls_handshake.c",
            "memcpy(buf, input, len);",
            intro_s1,
            feat1,
        ),
        Scenario(
            "S2 rename + small edit",
            "tls/tls_record.c",
            "memcpy(out, record, record_len);",
            intro_s2,
            feat2,
        ),
        Scenario(
            "S3 rename + >90% rewrite",
            "crypto/hash_engine.c",
            "return memcmp(a, b, DIGEST_LEN);",
            intro_s3,
            feat3,
        ),
    ]

    # Expected git behavior per scenario.
    expectations = {
        "S1 pure rename": {"is_rename": True, "follow": True, "origin_is_true": True},
        "S2 rename + small edit": {
            "is_rename": True,
            "follow": True,
            "origin_is_true": True,
        },
        "S3 rename + >90% rewrite": {
            "is_rename": False,
            "follow": False,
            "origin_is_true": False,
        },
    }

    for sc in scenarios:
        exp = expectations[sc.name]
        line = line_no(root, "main", sc.newpath, sc.anchor_line)

        status, pct = rename_score(root, sc.rename_commit, sc.newpath)
        is_rename = status.startswith("R")
        crosses = follow_crosses(root, sc.newpath, sc.true_intro)
        origin = log_l_origin(root, sc.newpath, line)
        origin_is_true = origin == sc.true_intro

        sc.results = {
            "anchor_line": line,
            "status": status,
            "similarity": pct,
            "is_rename": is_rename,
            "follow_crosses": crosses,
            "logL_origin": origin,
            "origin_is_true": origin_is_true,
        }

        sc.check(
            f"git diff classifies as {'rename' if exp['is_rename'] else 'delete+add'}",
            is_rename == exp["is_rename"],
            f"status={status} similarity={pct}",
        )
        sc.check(
            f"git log --follow {'crosses' if exp['follow'] else 'stops at'} the rename",
            crosses == exp["follow"],
            f"reaches true introducer {short(sc.true_intro)}: {crosses}",
        )
        sc.check(
            "git log -L origin "
            + ("== true introducer" if exp["origin_is_true"] else "!= true introducer"),
            origin_is_true == exp["origin_is_true"],
            f"origin={short(origin)} true={short(sc.true_intro)} "
            f"rename={short(sc.rename_commit)}",
        )

    # ----- End-to-end: run the ACTUAL bot pipeline ------------------------
    # Land a small CVE-style fix on each renamed file, then ask the bot for the
    # introducer the way it would in production.
    fixes = {}
    fixes["S1 pure rename"] = cve_fix(
        root,
        "crypto/tls_handshake.c",
        "    memcpy(buf, input, len);",
        "    if (len > MAX_HANDSHAKE) {\n        return;\n    }\n    memcpy(buf, input, len);",
        "fix: bounds check in process_handshake (cve-handshake)",
    )
    fixes["S2 rename + small edit"] = cve_fix(
        root,
        "tls/tls_record.c",
        "    memcpy(out, record, record_len);",
        "    if (record_len > MAX_RECORD) {\n        return;\n    }\n    memcpy(out, record, record_len);",
        "fix: length check in handle_record (cve-record)",
    )
    fixes["S3 rename + >90% rewrite"] = cve_fix(
        root,
        "crypto/hash_engine.c",
        "    return memcmp(a, b, DIGEST_LEN);",
        "    if (a == NULL || b == NULL) {\n        return -1;\n    }\n    return memcmp(a, b, DIGEST_LEN);",
        "fix: null guard in hash_compare (cve-digest)",
    )

    with chdir(root):
        for sc in scenarios:
            fix = fixes[sc.name]
            files = get_changed_files(fix)
            introducers = find_introducing_commit(fix, files)
            bot_correct = sc.true_intro in introducers
            sc.results["bot_files"] = files
            sc.results["bot_introducers"] = sorted(introducers)
            sc.results["bot_correct"] = bot_correct
            sc.check(
                "bot find_introducing_commit "
                + ("finds" if sc.name != "S3 rename + >90% rewrite" else "misses")
                + " the true introducer",
                bot_correct == (sc.name != "S3 rename + >90% rewrite"),
                f"introducers={[short(s) for s in sorted(introducers)]} "
                f"true={short(sc.true_intro)}",
            )

    print_report(scenarios)
    return all(sc.passed for sc in scenarios)


# ===========================================================================
# Reporting
# ===========================================================================


def print_report(scenarios):
    print("=" * 88)
    print("Rename-tracking results")
    print("=" * 88)

    for sc in scenarios:
        r = sc.results
        verdict = "PASS" if sc.passed else "FAIL"
        print(f"\n[{verdict}] {sc.name}  ->  {sc.newpath}")
        sim = f"{r['similarity']}%" if r["similarity"] is not None else "n/a"
        print(
            f"  rename detection (default 50%): {r['status']}"
            f"  | similarity (forced 1%): {sim}"
            f"  | git records a rename: {r['is_rename']}"
        )
        print(
            f"  git log --follow reaches the pre-rename introducer "
            f"{short(sc.true_intro)}: {r['follow_crosses']}"
        )
        print(
            f"  git log -L origin: {short(r['logL_origin'])}  "
            f"(true introducer {short(sc.true_intro)}, "
            f"rename commit {short(sc.rename_commit)})"
        )
        if "bot_introducers" in r:
            print(
                f"  bot find_introducing_commit -> "
                f"{[short(s) for s in r['bot_introducers']]} "
                f"| matches true introducer: {r['bot_correct']}"
            )
        for label, ok, detail in sc.checks:
            mark = "ok " if ok else "XX "
            print(f"    [{mark}] {label}" + (f"  ({detail})" if detail else ""))

    print("\n" + "=" * 88)
    print("Summary")
    print("=" * 88)
    print(
        f"  {'scenario':<28} {'rename?':<8} {'sim':<6} {'follow':<8} "
        f"{'bot-correct':<12} {'verdict'}"
    )
    print(f"  {'-' * 28} {'-' * 8} {'-' * 6} {'-' * 8} {'-' * 12} {'-' * 7}")
    for sc in scenarios:
        r = sc.results
        sim = f"{r['similarity']}%" if r["similarity"] is not None else "-"
        print(
            f"  {sc.name:<28} {str(r['is_rename']):<8} {sim:<6} "
            f"{str(r['follow_crosses']):<8} "
            f"{str(r.get('bot_correct', '-')):<12} "
            f"{'PASS' if sc.passed else 'FAIL'}"
        )

    print()
    print("Interpretation:")
    print("  - S1/S2: git's rename detection keeps the line history intact, so the")
    print("    bot traces fixes back to the original introducing commit. Backport")
    print("    impact analysis is correct across these renames.")
    print("  - S3: the rename + >90% rewrite lands below git's 50% similarity")
    print("    threshold, so git sees delete+add and the line history dead-ends at")
    print("    the rename commit. The bot reports the rename as the 'introducer',")
    print("    which is too recent -> branches that forked before the rewrite but")
    print("    after the real introducer can be wrongly judged unaffected (a silent")
    print("    FALSE NEGATIVE / missed backport).")
    print("  - Mitigation: lowering git's rename threshold (-M/--find-renames=<n>%)")
    print("    or splitting 'rename' and 'rewrite' into separate commits restores")
    print("    trackability.")


def main():
    ok = run_all()
    if ok:
        print("\nAll scenarios behaved as documented. ✅")
        sys.exit(0)
    print("\nOne or more scenarios deviated from the documented behavior. ❌")
    sys.exit(1)


if __name__ == "__main__":
    main()
