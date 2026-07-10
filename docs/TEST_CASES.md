# Backport Bench — Test Case Catalog

Every test case is a real `aws/aws-lc` mainline fix. Click a commit or PR
link to view it on GitHub. The **Verdict** column is the per-branch
repo-forensic ground truth (see `ground_truth.txt` for full evidence):

`P`=patched · `A`=affected (needs backport) · `-`=not affected · `?`=undetermined
across `21` `21-1MU` `22` `24` `25-lts` `NetOS`.

**36 fixes × 6 branches.** Regenerate: `python3 scripts/make_test_cases_md.py > TEST_CASES.md`

## CVE-tracked fixes

| CVE | Fix | Links | Verdict (21/21-1MU/22/24/25-lts/NetOS) |
|---|---|---|---|
| CVE-2023-3446 | DH_check() excessive time with oversized modulus | [`9545d9de6059`](https://github.com/aws/aws-lc/commit/9545d9de6059a94a7fd0e49a39b32905a7dd2f74) | 21:P 21-1MU:P 22:P 24:P 25-lts:P NetOS:P |
| CVE-2022-0778 | BN_mod_sqrt infinite loop | [`11b50d39cf23`](https://github.com/aws/aws-lc/commit/11b50d39cf2378703a4ca6b6fee9d76a2e9852d1) | 21:P 21-1MU:P 22:P 24:P 25-lts:P NetOS:P |
| CVE-2020-1971 | Fix potential NULL-dereference CVE-2020-1971 | [`bfd98fa038e0`](https://github.com/aws/aws-lc/commit/bfd98fa038e0c298bb85e497b77e5cbc83b99f76) · [PR #78](https://github.com/aws/aws-lc/pull/78) | 21:P 21-1MU:P 22:P 24:P 25-lts:P NetOS:P |
| CVE-2021-23841 | Improvement for CVE-2021-23841 | [`004549d28b88`](https://github.com/aws/aws-lc/commit/004549d28b887d15fd5b125350f02be5dfdfad2f) · [PR #92](https://github.com/aws/aws-lc/pull/92) | 21:P 21-1MU:P 22:P 24:P 25-lts:P NetOS:P |
| CVE-2023-3817 | Consistently reject large p and large q in DH | [`779d13f705c8`](https://github.com/aws/aws-lc/commit/779d13f705c8897bbc75354e3e63b055c846c78e) | 21:A 21-1MU:A 22:A 24:P 25-lts:P NetOS:P |

## Security & correctness fixes (aws-lc PR/issue tracked)

| CVE | Fix | Links | Verdict (21/21-1MU/22/24/25-lts/NetOS) |
|---|---|---|---|
| — | Excessive time checking DH q parameter value | [`1bb574f3f2e7`](https://github.com/aws/aws-lc/commit/1bb574f3f2e758124b2a7acac6550ec0c63d9970) · [PR #1121](https://github.com/aws/aws-lc/pull/1121) | 21:P 21-1MU:P 22:P 24:P 25-lts:P NetOS:P |
| — | pkcs8: cap ciphertext length before allocating | [`e17506cdbde1`](https://github.com/aws/aws-lc/commit/e17506cdbde19ce68e21681b2fb9581b1fe93037) | 21:P 21-1MU:P 22:P 24:P 25-lts:P NetOS:P |
| — | Only update thread_states_list if freed state is head (UAF, #1294) | [`3b5856603682`](https://github.com/aws/aws-lc/commit/3b58566036828fabedb9adfbeb08fc24b1f4883a) · [PR #1294](https://github.com/aws/aws-lc/pull/1294) | 21:P 21-1MU:P 22:P 24:P 25-lts:P NetOS:P |
| — | Kyber avoid compiler division (constant-time) | [`17cd6574bdb8`](https://github.com/aws/aws-lc/commit/17cd6574bdb89744df61920efbe49d383963d8a2) · [PR #1360](https://github.com/aws/aws-lc/pull/1360) | 21:- 21-1MU:- 22:P 24:P 25-lts:P NetOS:P |
| — | ML-DSA constant-time hardening | [`2a184bd568ff`](https://github.com/aws/aws-lc/commit/2a184bd568ff48617e5ed306d844b54d51aa590c) · [PR #2602](https://github.com/aws/aws-lc/pull/2602) | 21:- 21-1MU:- 22:- 24:- 25-lts:P NetOS:- |
| — | reject zero-sized digests in HKDF EVP_PKEY | [`8a43348a53b0`](https://github.com/aws/aws-lc/commit/8a43348a53b09abe017e00351f8963b6d1c76543) | 21:- 21-1MU:- 22:A 24:P 25-lts:P NetOS:P |
| — | Prevent non-constant-time code in Kyber-R3 and ML-KEM | [`4b07805bddc5`](https://github.com/aws/aws-lc/commit/4b07805bddc55f68e5ce8c42f215da51c7a4e099) · [PR #1619](https://github.com/aws/aws-lc/pull/1619) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:P |
| — | Reject XOF digests in DH_compute_key_hashed | [`110f184623b5`](https://github.com/aws/aws-lc/commit/110f184623b527439b14f3ad9a496191d54c32dc) | 21:A 21-1MU:A 22:A 24:P 25-lts:P NetOS:P |
| — | evp: disable EVP_PKEY_derive for KEM method | [`dcd1690320a3`](https://github.com/aws/aws-lc/commit/dcd1690320a362d5f0e0adae870a211431c9f7e9) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:- |
| — | Avoid mixing SSE and AVX in XTS-mode AVX512 | [`37c2b5e8cf5a`](https://github.com/aws/aws-lc/commit/37c2b5e8cf5a325beb6ab2abd5b2946bcfe01bde) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:A |
| — | Don't use expired certificates if possible | [`4e32cc53a81d`](https://github.com/aws/aws-lc/commit/4e32cc53a81d339567c83d163181fc4a590b59f0) | 21:A 21-1MU:A 22:P 24:P 25-lts:P NetOS:P |
| — | Remove retries on PCT failure in EC and RSA key generation | [`90d2a34ead8d`](https://github.com/aws/aws-lc/commit/90d2a34ead8d51c3a97be33f1e6eff751707807c) | 21:A 21-1MU:A 22:A 24:P 25-lts:P NetOS:A |
| — | evp: fix DSA keygen error-path UAF/double-free | [`cf2e09fb5d40`](https://github.com/aws/aws-lc/commit/cf2e09fb5d40cdd96e119c5ac64b509052e79aff) | 21:- 21-1MU:- 22:- 24:- 25-lts:P NetOS:- |
| — | Add return checks on SHA3 functions in ML-KEM | [`f89c9bec9aea`](https://github.com/aws/aws-lc/commit/f89c9bec9aea680839ae10aef2d9ac627a9c08c3) · [PR #1859](https://github.com/aws/aws-lc/pull/1859) | 21:? 21-1MU:? 22:? 24:P 25-lts:P NetOS:? |
| — | ML-KEM encaps key modulus check optimization | [`ed6d6ca3cf56`](https://github.com/aws/aws-lc/commit/ed6d6ca3cf561de706558277ac9a0c37db076060) · [PR #1874](https://github.com/aws/aws-lc/pull/1874) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:- |
| — | ML-KEM decapsulation key hash check | [`c5d3f3dd39df`](https://github.com/aws/aws-lc/commit/c5d3f3dd39df292b99f24412bbde11b5b3128948) · [PR #1873](https://github.com/aws/aws-lc/pull/1873) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:? |
| — | ML-KEM encapsulation key modulus check | [`2835116571e8`](https://github.com/aws/aws-lc/commit/2835116571e8607861ba8dd0be2676f2c5cfaaab) · [PR #1868](https://github.com/aws/aws-lc/pull/1868) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:? |
| — | Use constant curve-specific groups whenever possible | [`507120cc3dba`](https://github.com/aws/aws-lc/commit/507120cc3dba0640f28b797c1d1953f4b0b579b8) | 21:A 21-1MU:A 22:A 24:P 25-lts:P NetOS:P |
| — | Add a value barrier when checking for point doubling | [`8da095382457`](https://github.com/aws/aws-lc/commit/8da095382457d596d2e8240fad9fd2678a375973) | 21:A 21-1MU:A 22:A 24:P 25-lts:P NetOS:P |
| — | Make asserts constant-time too | [`28be5302b592`](https://github.com/aws/aws-lc/commit/28be5302b592d69425488b3bddef7c685e91de68) | 21:A 21-1MU:A 22:A 24:A 25-lts:P NetOS:A |
| — | Rand small fixes | [`b7917d9b83a4`](https://github.com/aws/aws-lc/commit/b7917d9b83a419a4da945ae63734b19b37778cd5) · [PR #2664](https://github.com/aws/aws-lc/pull/2664) | 21:- 21-1MU:- 22:- 24:- 25-lts:P NetOS:- |
| — | Fix PKCS12 Error Code | [`3cc65bd333ee`](https://github.com/aws/aws-lc/commit/3cc65bd333eef7e65d019e0af0e25a0da40640f4) · [PR #2538](https://github.com/aws/aws-lc/pull/2538) | 21:A 21-1MU:A 22:A 24:A 25-lts:P NetOS:A |
| — | Add new verification error for mismatched signatures | [`9ad27ab683af`](https://github.com/aws/aws-lc/commit/9ad27ab683afa084b096ee522161dc1ec24432f6) · [PR #413](https://github.com/aws/aws-lc/pull/413) | 21:A 21-1MU:A 22:P 24:P 25-lts:P NetOS:P |
| — | Fix theoretical overflow in BIO_printf | [`32e73b358236`](https://github.com/aws/aws-lc/commit/32e73b3582360abed73bd3ad0d2b74fcc1d4924c) · [PR #2369](https://github.com/aws/aws-lc/pull/2369) | 21:A 21-1MU:A 22:A 24:A 25-lts:P NetOS:A |
| — | OOB input read in AES-XTS Decrypt AVX-512 | [`eb0c0c0d8e4e`](https://github.com/aws/aws-lc/commit/eb0c0c0d8e4e94183a608b0403d899beb9d4b949) · [PR #2227](https://github.com/aws/aws-lc/pull/2227) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:A |
| — | 1-byte OOB read in EVP_PKEY_asn1_find_str | [`921c6465918e`](https://github.com/aws/aws-lc/commit/921c6465918e2d118533199e6e2959f82483dde5) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:- |
| — | Align X509 PARTIAL_CHAIN behavior with 1.1.1 | [`9fbfa706c9b7`](https://github.com/aws/aws-lc/commit/9fbfa706c9b7fece478605f57d4706b1d72c7b8e) · [PR #1917](https://github.com/aws/aws-lc/pull/1917) | 21:- 21-1MU:- 22:P 24:P 25-lts:P NetOS:? |
| — | Handle ChaCha20 counter overflow consistently | [`a1aadda437e6`](https://github.com/aws/aws-lc/commit/a1aadda437e685b698aae21d311fed0ed68bf576) | 21:A 21-1MU:A 22:A 24:P 25-lts:P NetOS:P |
| — | Plug a leak in ASN1_item_i2d() | [`2f55cf36eda8`](https://github.com/aws/aws-lc/commit/2f55cf36eda864ab9562bf495170915d3a56474f) | 21:- 21-1MU:- 22:? 24:P 25-lts:P NetOS:P |
| — | Avoid conversion overflow from struct tm | [`6c21187fbd56`](https://github.com/aws/aws-lc/commit/6c21187fbd5699b2b3d924bef5abaa901a0a82af) | 21:A 21-1MU:A 22:A 24:P 25-lts:P NetOS:A |
| — | Fix CRL distribution point scope check in crl_crldp_check | [`47389586f8aa`](https://github.com/aws/aws-lc/commit/47389586f8aa77c83245173793f4d44ed1d6c3a8) · [PR #3105](https://github.com/aws/aws-lc/pull/3105) | 21:- 21-1MU:- 22:- 24:P 25-lts:P NetOS:P |
