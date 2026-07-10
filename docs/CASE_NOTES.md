# Case Notes — descriptor-grounded understanding

Built by reading each fix's **commit message / PR body** on aws/aws-lc (the real
descriptors), not just mechanical git signals. This is the "what is actually going
on" layer that sits on top of `ground_truth.txt` (per-branch verdicts) and
`TEST_CASES.md` (links).

Severity source key: **CVE** = public CVE · **AISLE** = reported by Joshua Rogers /
AISLE Research Team · **ticket** = internal Amazon security/tracking id · **hardening**
= defense-in-depth / const-time · **hygiene** = leak/robustness cleanup · **compat** =
behavior alignment.

---

## Tier 1 — CVEs / external security reports

| Fix | What the descriptor says | Source |
|---|---|---|
| `11b50d39cf23` | Infinite loop in `BN_mod_sqrt()` | **CVE-2022-0778** |
| `bfd98fa038e0` | NULL-deref in `GENERAL_NAME_cmp` | **CVE-2020-1971** |
| `004549d28b88` | X509 hash-collision handling | **CVE-2021-23841** |
| `9545d9de6059` | `DH_check()` excessive time, oversized modulus | **CVE-2023-3446** (#1109) |
| `1bb574f3f2e7` | Excessive time checking DH q parameter | CVE-2023-3446 family (#1121) |
| `779d13f705c8` | Reject large p/q in DH (attacker-supplied params) | **CVE-2023-3817** |
| `e17506cdbde1` | `pkcs8_pbe_decrypt` allocates from attacker-influenced ASN.1 length before the INT_MAX check | **AISLE** |
| `8a43348a53b0` | Divide-by-zero in `HKDF_expand` if XOF digest selected (`EVP_MD_size<=0`) | **AISLE** |
| `921c6465918e` | 1-byte OOB **read** in `EVP_PKEY_asn1_find_str` length calc | **AISLE** |
| `110f184623b5` | OOB **write**: `DH_compute_key_hashed` passes uninit `out_len`; XOF `EVP_DigestFinalXOF` treats it as input length | (memory safety) |

## Tier 2 — memory-safety / type-safety (internal-found)

| Fix | What the descriptor says |
|---|---|
| `cf2e09fb5d40` | DSA keygen error-path **UAF/double-free**: DSA assigned into EVP_PKEY then freed on failure → dangling pointer |
| `dcd1690320a3` | KEM EVP method **type confusion**: `derive` wrongly pointed at HKDF derive → `ctx->data` KEM_PKEY_CTX vs HKDF_PKEY_CTX |
| `eb0c0c0d8e4e` | **OOB read** in AES-XTS Decrypt AVX-512 16-block loop (#2227) |
| `48040c6ee15a` | Type safety + bounds in **AES-GCM and AES-CCM** ctrl handlers (the GCM `SET_IV_INV` `arg` is unchecked → OOB) (#3034) — *descriptor confirms it touches e_aes.c GCM, validating the AFFECTED correction* |
| `5039544f9dbc` | INT_MAX bounds check before `EVP_CipherUpdate` in PKCS8/PKCS12 (#3043) |
| `a48830c0a90d` | `PKCS8_decrypt` mishandles negative `pass_len` (only `== -1`) (#3039) |
| `32e73b358236` | Theoretical overflow in `BIO_printf` (INT_MAX+1 scratch buffer) (#2369) |
| `40b080b7ddc1` | Integer overflow in `x509v3_bytes_to_hex` + OCSP print NULL checks (#3127) |
| `6c21187fbd56` | Conversion overflow from `struct tm` (int fields) |
| `a1aadda437e6` | ChaCha20 counter-overflow handling consistency |

## Tier 3 — leaks / robustness hygiene

| Fix | What the descriptor says |
|---|---|
| `80f0e5780c88` | HMAC error paths: leaks, state bugs, missing cleansing (#3081) |
| `e0cf5f83821d` | KEM_KEY setters overwrite `public_key`/`secret_key` without freeing → leak (#3041) |
| `04e7dc0cd986` | `PKCS7_verify` frees only BIO chain head, not whole chain → leak (#3036) |
| `2f55cf36eda8` | `ASN1_item_i2d` leaks `buf` on `ASN1_item_ex_i2d` error |
| `c21d40da4bbe` | ACVP modulewrapper `RSADecryptionPrimitiveCRT` leaks 8 BIGNUMs/call — **TEST-TOOL file** (#3094) |
| `3b5856603682` | UAF/NULL-deref in `thread_states_list` (only update if freed state is head) (#1294) |
| `f295f20a1d3d` | PQDSA_KEY setters leave key in inconsistent state (#3040) |
| `0430b7d02f3d` | PQDSA_KEY set_raw → goto-err cleanup, fix reuse leaks (#2993) |

## Tier 4 — PQ correctness / const-time hardening

| Fix | What the descriptor says |
|---|---|
| `2a184bd568ff` | ML-DSA const-time hardening (caddq, poly_chknorm, decompose) (#2602) |
| `cb87f254eb15` | ML-DSA `poly_uniform` SHAKE-squeeze bug (#2721) |
| `4b07805bddc5` | Non-const-time branch in Kyber-R3/ML-KEM `poly_frommsg` (ticket V1399146249) |
| `17cd6574bdb8` | Kyber avoid compiler DIV (const-time) (#1360) |
| `f89c9bec9aea` | SHA3 return checks in ML-KEM (ticket P155314914) (#1859) |
| `c5d3f3dd39df` | ML-KEM decapsulation key hash check, FIPS 203 §7.3 (#1873) |
| `2835116571e8` | ML-KEM encapsulation key modulus check, FIPS 203 §7.2 (#1868) |
| `ed6d6ca3cf56` | ML-KEM encaps modulus check optimization (#1874) |
| `28be5302b592` | Make asserts constant-time (defense-in-depth) |
| `507120cc3dba` | Constant curve-specific groups (EC side-channel) |
| `8da095382457` | Value barrier on EC point-doubling branch (side-channel) |

## Tier 5 — behavior/compat/FIPS + one cosmetic

| Fix | What the descriptor says |
|---|---|
| `90d2a34ead8d` | FIPS: no retries on PCT failure — enter error state (#1938) |
| `4e32cc53a81d` | Don't use expired certs if a valid one follows in the stack (#1282) |
| `9fbfa706c9b7` | Align X509 PARTIAL_CHAIN with 1.1.1 (#1917) |
| `8e7e4a569db4` | Reject parameterless DSA keys in SPKIs (BoringSSL) (#3057) |
| `3cc65bd333ee` | Correct PKCS12 error code (ticket CryptoAlg-3347) (#2538) |
| `9ad27ab683af` | New verification error for mismatched signatures (#413) |
| `ec83c51b5b35` | Correct BIO mem buffer type in `mem_ctrl` (#3204) |
| `f8d3b92210f4` | `BIO_ADDR_rawmake` AF_UNIX `wherelen` handling (ticket V2196133741) (#3233) |
| `37c2b5e8cf5a` | AVX-512 XTS perf (movdqa→vmovdqa) — **performance, not security** (#2140) |
| `efb6ba943437` | `pass_util.cc` password handling — **TEST-TOOL** file (#3032) |
| `b7917d9b83a4` | **"leftover comments and cleaning up language"** — COSMETIC, not a real fix (#2664) |

---

## What this review changed / flagged

1. **`48040c` correction validated** — the PR body explicitly says it fixes the AES-GCM
   ctrl handler (`e_aes.c`), confirming the older branches (which carry the unguarded
   `SET_IV_INV` memcpy) are genuinely AFFECTED. The AI was right; the earlier
   NOT_AFFECTED verdict was wrong.
2. **`b7917d9b` is cosmetic** ("leftover comments and cleaning up language") — not a
   security/correctness fix. It's a weak bench entry and a candidate to drop.
3. **`37c2b5e8` is a performance fix**, not security (perf drop from SSE/AVX mixing) —
   still a valid backport-decision test, but categorize it as perf, not a vuln.
4. **`c21d40` and `efb6ba94` are test-tool files** (`util/fipstools/...`,
   `tool-openssl/...`), already treated as UNDETERMINED / not-affected — consistent with
   the descriptors.
5. The methodology fix stands: determine AFFECTED/NOT_AFFECTED at the **construct**
   level (the specific vulnerable function/pattern named in the descriptor), never by
   file presence or a generic pre-image string.
