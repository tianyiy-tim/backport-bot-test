#include <string.h>

#define MAX_COPY 65536

void copy_buffer(char *dst, const char *src, int len) {
    if (dst == NULL || src == NULL || len < 0) {
        return;
    }
    if (len > MAX_COPY) {
        return;
    }
    memcpy(dst, src, len);
}

int buffer_length(const char *buf) {
    if (buf == NULL) {
        return 0;
    }
    return (int)strlen(buf);
}
