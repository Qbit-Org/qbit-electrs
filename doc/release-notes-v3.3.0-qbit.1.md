# qbit-electrs v3.3.0-qbit.1 Release Notes

`v3.3.0-qbit.1` is the first public qbit-aware release of electrs, based on
`mempool/electrs v3.3.0`. These notes describe the qbit-specific delta from
that upstream baseline.

## Highlights

- Added qbit network support for mainnet, testnet4, and regtest through
  `--network qbit`, `--network qbittestnet4`, and `--network qbitregtest`.
- Added qbit-aware block, transaction, and address handling, including P2MR
  addresses and scripts, qbit Bech32m and Base58 prefixes, WSF=1 size
  semantics, pure 80-byte headers, and AuxPoW block payload parsing.
- Preserved the upstream REST and Electrum API shape while extending qbit
  behavior for headers, blocks, addresses, UTXOs, mempool views, transaction
  broadcast, fee estimates, and proofs.
- Added Docker Compose support for running qbit-electrs against an external
  `qbitd`, defaulting to qbit testnet4.
- Added deterministic qbit fixtures, a local qbit regtest harness, and a
  read-only qbit testnet4 canary for parity checks against qbit RPC.

## qbit Behavior

- qbit transaction `size`, `weight`, and `vsize` use witness-inclusive
  serialized byte length under `WITNESS_SCALE_FACTOR = 1`.
- REST and Electrum header endpoints expose the pure 80-byte qbit block header,
  not AuxPoW-extended header bytes.
- Raw qbit block responses preserve AuxPoW payload bytes by fetching raw blocks
  from the daemon when needed.
- P2MR outputs are reported as `v2_p2mr`, with qbit opcode names surfaced in
  script assembly.
- qbit fee estimate targets remain block-count based and use qbit's 60-second
  aggregate block cadence for wall-clock interpretation.

## Operator Notes

- qbit-electrs expects an already-running, unpruned `qbitd`; the container does
  not bundle or start `qbitd`.
- Full witness history is required. qbitd witness-pruned mode is not suitable
  for qbit-electrs ingestion or validation.
- Docker Compose defaults to `qbittestnet4`, RPC port `48352`, REST port
  `3004`, Electrum port `40001`, and monitoring port `44224`.
- qbit Electrum peer discovery is intentionally disabled in this v1 release;
  plain Electrum JSON-RPC service remains supported.
- Inherited Bitcoin and Liquid modes remain compile-compatible, but qbit
  correctness is scoped to non-Liquid qbit networks.

## Validation

- Added qbit codec, P2MR address and script, AuxPoW, fee and size, mempool,
  REST, Electrum, fixture-manifest, and promoted-fixture regression coverage.
- Added a regtest harness that builds or downloads compatible qbit binaries,
  mines qbit regtest data, syncs qbit-electrs, and checks REST/Electrum parity
  against qbitd.
- Added a live testnet4 canary for read-only parity checks against an archive
  qbit node.
- CI now includes qbit harness coverage in addition to inherited Rust checks.
