#include <string.h>

#define MAX_RECORD 16384

// Fixed: length + null
void handle_record(char *out, const char *record, int record_len) {
    if (out == NULL || record == NULL) {
        return;
    }
    if (record_len > MAX_RECORD) {
        return;
    }
    memcpy(out, record, record_len);
}
