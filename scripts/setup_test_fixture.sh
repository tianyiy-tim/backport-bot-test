#!/usr/bin/env bash
#
# Rebuilds the test repo with a richer commit history for exercising the bot.
#
# History layout:
#
#   G. fix: refactor + null-check (crypto.c + tls.c)   <- main           [tag: cve-multi]
#   F. fix: length validation in handle_record (tls.c)                   [tag: cve-tls]
#   E. fix: bounds check in process_handshake (crypto.c)                 [tag: cve-crypto]
#   D. add cert.c                                       <- FIPS-2024 (+ divergent commit for conflict testing)
#   C. add tls.c with handle_record                     <- FIPS-2023
#   B. add crypto.c with process_handshake              <- FIPS-2022
#   A. initial commit (app.c)                           <- FIPS-2021
#
# WARNING: this rewrites history and force-pushes. Test repos only.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Sanity: make sure we're in the right repo
if ! git remote get-url origin | grep -q backport-bot-test; then
  echo "Refusing to run: origin doesn't look like backport-bot-test" >&2
  exit 1
fi

# Preserve untracked files (scripts/, .github/) by stashing them out of the way.
# Reset doesn't touch untracked files so this is mostly defensive.
WORK=$(mktemp -d)
echo "Backing up untracked work to $WORK"
[ -d scripts ] && cp -R scripts "$WORK/"
[ -d .github ] && cp -R .github "$WORK/"

# Wipe local refs we're about to rewrite
git checkout --quiet --detach
for b in main AWS-LC-FIPS-2021 AWS-LC-FIPS-2022 AWS-LC-FIPS-2023 AWS-LC-FIPS-2024; do
  git branch -D "$b" 2>/dev/null || true
done
for t in cve-crypto cve-tls cve-multi; do
  git tag -d "$t" 2>/dev/null || true
done

# Wipe everything tracked so we can rebuild from a clean slate
git rm -rf --quiet --ignore-unmatch . || true
rm -f app.c crypto.c tls.c cert.c

# Restore untracked files into place (they survived but let's be explicit)
[ -d "$WORK/scripts" ] && cp -R "$WORK/scripts" .
[ -d "$WORK/.github" ] && cp -R "$WORK/.github" .

# ---------------------------------------------------------------------------
# Commit A: initial — app.c only
# ---------------------------------------------------------------------------
cat > app.c <<'EOF'
int main() { return 0; }
EOF
git checkout --orphan rebuild
git rm -rf --quiet --cached . 2>/dev/null || true
git add app.c
git commit --quiet -m "initial commit"
git tag _A

# ---------------------------------------------------------------------------
# Commit B: add crypto.c with vulnerable process_handshake
# ---------------------------------------------------------------------------
cat > crypto.c <<'EOF'
#include <string.h>

#define MAX_BUF_SIZE 4096

void process_handshake(char *buf, const char *input, int len) {
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
EOF
git add crypto.c
git commit --quiet -m "add crypto.c with process_handshake"
git tag _B

# ---------------------------------------------------------------------------
# Commit C: add tls.c with vulnerable handle_record
# ---------------------------------------------------------------------------
cat > tls.c <<'EOF'
#include <string.h>

#define MAX_RECORD_SIZE 16384

void handle_record(char *out, const char *record, int record_len) {
    memcpy(out, record, record_len);
}
EOF
git add tls.c
git commit --quiet -m "add tls.c with handle_record"
git tag _C

# ---------------------------------------------------------------------------
# Commit D: add cert.c (no vulnerability)
# ---------------------------------------------------------------------------
cat > cert.c <<'EOF'
int parse_cert(const char *cert_data, int len) {
    return 1;
}
EOF
git add cert.c
git commit --quiet -m "add cert.c with parse_cert"
git tag _D

# ---------------------------------------------------------------------------
# Commit E: fix bounds check in process_handshake (single-file CVE)
# ---------------------------------------------------------------------------
cat > crypto.c <<'EOF'
#include <string.h>

#define MAX_BUF_SIZE 4096

// Fixed: added bounds check
void process_handshake(char *buf, const char *input, int len) {
    if (len > MAX_BUF_SIZE) {
        return;
    }
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
EOF
git add crypto.c
git commit --quiet -m "fix: add bounds check to process_handshake (CVE-2026-99999)"
git tag _E
git tag cve-crypto

# ---------------------------------------------------------------------------
# Commit F: fix length validation in handle_record (single-file CVE)
# ---------------------------------------------------------------------------
cat > tls.c <<'EOF'
#include <string.h>

#define MAX_RECORD_SIZE 16384

// Fixed: validate record length
void handle_record(char *out, const char *record, int record_len) {
    if (record_len > MAX_RECORD_SIZE) {
        return;
    }
    memcpy(out, record, record_len);
}
EOF
git add tls.c
git commit --quiet -m "fix: validate record length in handle_record (CVE-2026-99998)"
git tag _F
git tag cve-tls

# ---------------------------------------------------------------------------
# Commit G: combined refactor + null-check (multi-file fix)
# ---------------------------------------------------------------------------
cat > crypto.c <<'EOF'
#include <string.h>

#define MAX_BUF_SIZE 4096

// Fixed: added bounds check + null check
void process_handshake(char *buf, const char *input, int len) {
    if (buf == NULL || input == NULL) {
        return;
    }
    if (len > MAX_BUF_SIZE) {
        return;
    }
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    if (sig == NULL) {
        return 0;
    }
    return 1;
}
EOF
cat > tls.c <<'EOF'
#include <string.h>

#define MAX_RECORD_SIZE 16384

// Fixed: validate record length + null check
void handle_record(char *out, const char *record, int record_len) {
    if (out == NULL || record == NULL) {
        return;
    }
    if (record_len > MAX_RECORD_SIZE) {
        return;
    }
    memcpy(out, record, record_len);
}
EOF
git add crypto.c tls.c
git commit --quiet -m "fix: add null-check guards across crypto.c and tls.c (CVE-2026-99997)"
git tag _G
git tag cve-multi

# ---------------------------------------------------------------------------
# Move main to G
# ---------------------------------------------------------------------------
git branch -f main _G

# ---------------------------------------------------------------------------
# Place release branches at their target commits
# ---------------------------------------------------------------------------
git branch -f AWS-LC-FIPS-2021 _A
git branch -f AWS-LC-FIPS-2022 _B
git branch -f AWS-LC-FIPS-2023 _C
git branch -f AWS-LC-FIPS-2024 _D

# ---------------------------------------------------------------------------
# Add a divergent commit on AWS-LC-FIPS-2024 so cherry-picking the crypto
# fix (E) onto it will produce a CONFLICT — useful for testing later.
#
# The divergence: this branch rewrote process_handshake to use strncpy
# instead of memcpy. The fix on main adds a bounds check before memcpy,
# but on this branch the surrounding code uses strncpy, so the patch
# context won't match cleanly.
# ---------------------------------------------------------------------------
git checkout --quiet AWS-LC-FIPS-2024
cat > crypto.c <<'EOF'
#include <string.h>

#define MAX_BUF_SIZE 4096

void process_handshake(char *buf, const char *input, int len) {
    strncpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
EOF
git add crypto.c
git commit --quiet -m "refactor: switch process_handshake to strncpy"
git checkout --quiet main

# ---------------------------------------------------------------------------
# Clean up internal tags
# ---------------------------------------------------------------------------
for t in _A _B _C _D _E _F _G; do git tag -d "$t" >/dev/null; done
git branch -D rebuild 2>/dev/null || true

# ---------------------------------------------------------------------------
# Force-push everything
# ---------------------------------------------------------------------------
echo
echo "=== Local rebuild complete. Pushing to origin... ==="
git push --force origin main
git push --force origin AWS-LC-FIPS-2021
git push --force origin AWS-LC-FIPS-2022
git push --force origin AWS-LC-FIPS-2023
git push --force origin AWS-LC-FIPS-2024
git push --force origin cve-crypto cve-tls cve-multi

echo
echo "=== Done. New layout: ==="
git --no-pager log --all --oneline --graph --decorate
