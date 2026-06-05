#include <string.h>

void copy_buffer(char *dst, const char *src, int len) {
    memcpy(dst, src, len);
}

int buffer_length(const char *buf) {
    return (int)strlen(buf);
}
