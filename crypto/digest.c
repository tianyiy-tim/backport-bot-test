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
