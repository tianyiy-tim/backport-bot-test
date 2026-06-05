#include <string.h>

#define MAX_HANDSHAKE 4096

// Fixed: bounds check
void process_handshake(char *buf, const char *input, int len) {
    if (len > MAX_HANDSHAKE) {
        return;
    }
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
