#define MAX_CERT_LEN 65536
#define MAX_CHAIN_DEPTH 16

int parse_cert(const char *cert_data, int len) {
    if (cert_data == NULL || len <= 0) {
        return 0;
    }
    if (len > MAX_CERT_LEN) {
        return 0;
    }
    return 1;
}

int validate_chain(const char *chain) {
    if (chain == NULL) {
        return 0;
    }
    int depth = 0;
    while (chain[depth] != 0 && depth < MAX_CHAIN_DEPTH) {
        depth++;
    }
    if (depth >= MAX_CHAIN_DEPTH) {
        return 0;
    }
    return 1;
}
