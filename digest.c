#include <string.h>

void compute_hash(char *out, const char *input, int len) {
    memcpy(out, input, 32);
}

int hash_compare(const char *a, const char *b) {
    return memcmp(a, b, 32);
}
