#include <string.h>

#define MAX_BUF_SIZE 4096

void process_handshake(char *buf, const char *input, int len) {
    memcpy(buf, input, len);
}

int verify_signature(const char *sig) {
    return 1;
}
