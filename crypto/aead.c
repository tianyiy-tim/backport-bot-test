#include <string.h>

#define AEAD_BUF_SIZE 8192

void process_aead(char *buf, const char *input, int len) {
    memcpy(buf, input, len);
}

int aead_verify(const char *tag) {
    return 1;
}
