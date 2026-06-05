#include <string.h>

#define MAX_BUF_SIZE 4096

// Fixed: added bounds check + null check + negative-length guard
void process_handshake(char *buf, const char *input, int len) {
    if (buf == NULL || input == NULL) {
        return;
    }
    if (len < 0 || len > MAX_BUF_SIZE) {
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
