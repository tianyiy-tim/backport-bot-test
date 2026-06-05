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
