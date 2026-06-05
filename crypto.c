#include <string.h>

#define MAX_HANDSHAKE 4096

void process_handshake(char *buf, const char *input, int len) {
    strncpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
