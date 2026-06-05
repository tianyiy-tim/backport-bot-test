#define MAX_CERT_SIZE 16384

int parse_cert(const char *cert_data, int len) {
    if (len <= 0) {
        return 0;
    }
    if (len > MAX_CERT_SIZE) {
        return 0;
    }
    return 1;
}
