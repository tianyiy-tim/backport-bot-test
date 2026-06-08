#include <string.h>

#define AEAD_BUF_SIZE 8192

// Fixed: bounds check + null guard
void process_aead(char *buf, const char *input, int len) {
    if (buf == NULL || input == NULL) {
        return;
    }
    if (len < 0 || len > AEAD_BUF_SIZE) {
        return;
    }
    memcpy(buf, input, len);
}

int aead_verify(const char *tag) {
    if (tag == NULL) {
        return 0;
    }
    return 1;
}
