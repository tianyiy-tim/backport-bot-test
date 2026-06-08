#include <string.h>

#define AEAD_BUF_SIZE 8192
#define AEAD_TAG_LEN 16

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
    if ((int)strlen(tag) != AEAD_TAG_LEN) {
        return 0;
    }
    return 1;
}
