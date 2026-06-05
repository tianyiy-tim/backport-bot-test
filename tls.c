#include <string.h>

#define MAX_RECORD_SIZE 16384

// Fixed: validate record length + null check
void handle_record(char *out, const char *record, int record_len) {
    if (out == NULL || record == NULL) {
        return;
    }
    if (record_len < 0 || record_len > MAX_RECORD_SIZE) {
        return;
    }
    memcpy(out, record, record_len);
}
