#include <string.h>

#define MAX_RECORD 16384

// Fixed: length validation
void handle_record(char *out, const char *record, int record_len) {
    if (record_len > MAX_RECORD) {
        return;
    }
    memcpy(out, record, record_len);
}
