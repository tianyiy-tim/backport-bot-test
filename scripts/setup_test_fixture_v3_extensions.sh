#!/usr/bin/env bash
#
# v3 fixture extensions — adds edge-case CVE scenarios on top of v2.
#
# Adds these new tagged commits on main:
#   M. cve-pure-modification: changes existing lines without adding/removing
#   N. cve-pure-deletion:     removes vulnerable code
#   O. cve-cross-era:         touches utils/buffer.c (oldest) + crypto/digest.c (newer)
#
# Run AFTER setup_test_fixture_v2.sh has been run.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# Sanity check: are we in the v2 state?
if ! git rev-parse cve-record-multifile >/dev/null 2>&1; then
  echo "Refusing to run: cve-record-multifile tag missing. Run setup_test_fixture_v2.sh first." >&2
  exit 1
fi

git checkout --quiet main

# Remove any existing extension tags
for t in cve-pure-modification cve-pure-deletion cve-cross-era; do
  git tag -d "$t" 2>/dev/null || true
  git push --delete origin "$t" 2>/dev/null || true
done

# ============================================================================
# M. cve-pure-modification: changes existing line content (no add/remove)
# Modifies hash_compare to use constant-time comparison.
# ============================================================================
cat > crypto/digest.c <<'EOF'
#include <string.h>

void compute_hash(char *out, const char *input, int len) {
    memcpy(out, input, 32);
}

int hash_compare(const char *a, const char *b) {
    int diff = 0;
    for (int i = 0; i < 32; i++) {
        diff |= a[i] ^ b[i];
    }
    return diff;
}
EOF
git add crypto/digest.c
git commit --quiet -m "fix: constant-time hash_compare (cve-pure-modification)"
git tag cve-pure-modification

# ============================================================================
# N. cve-pure-deletion: removes a deprecated vulnerable code path
# Strips the verify_signature stub entirely (fix = remove the bug).
# ============================================================================
cat > crypto/handshake.c <<'EOF'
#include <string.h>

#define MAX_HANDSHAKE 4096

// Fixed: bounds check + null check
void process_handshake(char *buf, const char *input, int len) {
    if (buf == NULL || input == NULL) {
        return;
    }
    if (len > MAX_HANDSHAKE) {
        return;
    }
    memcpy(buf, input, len);
}
EOF
git add crypto/handshake.c
git commit --quiet -m "fix: remove vulnerable verify_signature stub (cve-pure-deletion)"
git tag cve-pure-deletion

# ============================================================================
# O. cve-cross-era: touches utils/buffer.c (commit A) AND crypto/digest.c (commit E)
# Stress-tests the multi-introducer pattern with introducers from very
# different points in history.
# ============================================================================
cat > utils/buffer.c <<'EOF'
#include <string.h>

void copy_buffer(char *dst, const char *src, int len) {
    if (dst == NULL || src == NULL || len < 0) {
        return;
    }
    memcpy(dst, src, len);
}

int buffer_length(const char *buf) {
    if (buf == NULL) {
        return 0;
    }
    return (int)strlen(buf);
}
EOF
cat > crypto/digest.c <<'EOF'
#include <string.h>

void compute_hash(char *out, const char *input, int len) {
    if (out == NULL || input == NULL || len < 32) {
        return;
    }
    memcpy(out, input, 32);
}

int hash_compare(const char *a, const char *b) {
    int diff = 0;
    for (int i = 0; i < 32; i++) {
        diff |= a[i] ^ b[i];
    }
    return diff;
}
EOF
git add utils/buffer.c crypto/digest.c
git commit --quiet -m "fix: harden buffer + digest input validation (cve-cross-era)"
git tag cve-cross-era

# ============================================================================
# Push extensions
# ============================================================================
echo
echo "=== Pushing extension commits to origin... ==="
git push origin main
git push origin cve-pure-modification cve-pure-deletion cve-cross-era

echo
echo "=== Done. Extension layout: ==="
git --no-pager log --oneline -8 main
