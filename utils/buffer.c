#include <string.h>

void copy_buffer(char *dst, const char *src, int len) {
    if (dst == NULL || src == NULL) {
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
