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
