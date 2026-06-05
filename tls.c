#include <string.h>

#define MAX_RECORD_SIZE 16384

void handle_record(char *out, const char *record, int record_len) {
    memcpy(out, record, record_len);
}
