# qbit Contract

This document is the checked-in qbit v1 contract for qbit-electrs work. Keep
network constants here authoritative for this repository; fixture manifests and
implementation docs should link here instead of copying the same tables.

Source:

- qbit source: `Qbit-Org/qbit` public release snapshot
- qbit source ref: `pinned snapshot` at `57bb53575f0d4931e77ac4a34b7e7f4c049f0636`
- qbit release tag: `v0.1.0-testnet4`
- Fixture manifests and harness outputs preserve source provenance for
  reproducibility.

The first v1 implementation targets are `testnet4` and `regtest`. Mainnet
constants are recorded because parsers and fixture checks need the complete
network contract, but mainnet activation is not the first integration target.

## Feature Policy

qbit-electrs v1 correctness is scoped to the non-Liquid qbit networks: `qbit`,
`qbittestnet4`, and `qbitregtest`.

The inherited `liquid` feature remains compile-compatible for upstream lineage
and regression coverage, but Liquid is not part of qbit correctness acceptance
criteria unless a future compatibility update explicitly adds it.

The inherited `electrum-discovery` feature also remains compile-compatible.
qbit v1 does not have public Electrum seed infrastructure, so qbit-electrs does
not start discovery for qbit networks even when discovery CLI options are
provided. Plain Electrum JSON-RPC service remains in scope.

Current CI keeps Liquid and Electrum-discovery feature combinations as compile
and lint regression checks. qbit behavior is proven by non-Liquid qbit tests and,
once available, the qbitd integration harness.

## Codec And Dependency Strategy

qbit-electrs v1 uses electrs-local qbit modules on top of the inherited
`rust-bitcoin` dependency. It does not use a separate `rust-bitcoin-qbit` fork
for this implementation.

The reason is boundary-specific: qbit transactions remain standard BIP-141
transactions, so `rust-bitcoin` transaction types can preserve witness data.
qbit blocks are the incompatible wire shape because AuxPoW payload bytes can
appear between the pure 80-byte header and the transaction vector. Therefore
raw qbit block and AuxPoW-extended header bytes must enter through
`src/qbit_codec.rs` before electrs converts them into normalized
`rust-bitcoin` block/header/transaction types for indexing.

The qbit-specific address and script surface lives in electrs-local helpers such
as `src/qbit_address.rs`, `Network::{Qbit,QbitTestnet4,QbitRegtest}`, qbit-aware
script rendering, and WSF=1 size/weight functions. Cargo dependency changes for
qbit consensus primitives are intentionally serialized: do not add a parallel
`rust-bitcoin-qbit` fork or change the shared codec strategy without a focused
contract update, fixture refresh, and execution-plan update.

## Networks

| Network | Magic bytes | Rust little-endian `u32` magic | P2P port | RPC port | Bech32 HRP | Genesis hash |
| --- | --- | --- | ---: | ---: | --- | --- |
| mainnet | `44 4f 24 a8` | `0xA8244F44` | 8355 | 8352 | `qb` | `0000324188278d089b5eabd9b62bf874c7512677cea90720af51ea5a61a2f997` |
| testnet4 | `c7 c4 16 40` | `0x4016C4C7` | 48355 | 48352 | `tq` | `000000000000796fe86bbc0bf1b66a07e4b4c0676f74b54cf7e5ce8b3f1a0090` |
| regtest | `a6 6b 1f da` | `0xDA1F6BA6` | 18460 | 18452 | `qbrt` | `0ee96aa77c4b600850e349344fa21b107e805f5370ddc7a6189db12cf69acce6` |

qbit source references at the source snapshot:

- Network magic, ports, HRPs, and genesis hashes: `src/kernel/chainparams.cpp`
  and `src/chainparamsbase.cpp`
- The mainnet genesis in this qbit ref is still marked in source as a
  development/pre-launch placeholder; do not infer launch readiness from this
  table.
- The testnet4 row is the public testnet4 reset at this qbit ref: genesis time
  `1781704709`, nonce `2528738861`, bits `0x1a7f1ab5`, merkle root
  `66bf018c3377135cdc87f66ed4926b6a3be5aeef890841a3cfebaff9dfb91ed0`.

qbit Base58 address prefixes at the source snapshot are:

| Network | P2PKH prefix | P2SH prefix |
| --- | ---: | ---: |
| mainnet | 58 (`Q`) | 63 (`S`) |
| testnet4 | 120 (`q`) | 125 (`s`) |
| regtest | 120 (`q`) | 125 (`s`) |

## Consensus Constants

| Constant | Value |
| --- | --- |
| `WITNESS_SCALE_FACTOR` | 1 |
| Maximum serialized block size | 2,000,000 bytes |
| Maximum block weight | 2,000,000 |
| Coinbase maturity | 1000 blocks |
| Aggregate block spacing target | 60 seconds |

With `WITNESS_SCALE_FACTOR = 1`, block weight equals witness-inclusive
serialized size. qbit-electrs should not reuse Bitcoin assumptions that discount
witness data when validating qbit block-size facts.

## Size, Vsize, And Fee-Rate Semantics

For qbit REST transaction responses, `size`, `weight`, and `vsize` all use the
witness-inclusive serialized transaction byte length. This mirrors qbit tx JSON
semantics under `WITNESS_SCALE_FACTOR = 1`.

For electrs mempool backlog and `mempool.get_fee_histogram`, the initial qbit
port uses the same byte-size denominator. qbitd's verbose mempool-entry `vsize`
can be bytes-per-sigop adjusted for high-sigop transactions and may exceed
serialized size; `regtest-qbitd-p2wsh-sigop-adjusted-vsize-spend-tx` captures
that qbitd behavior with a 115-byte transaction whose mempool `vsize` is 4440.
qbit-electrs v1 deliberately keeps REST tx size fields, `/mempool` backlog
`vsize`, and `mempool.get_fee_histogram` on the serialized-byte denominator so
large witness data is not discounted. Matching qbitd's sigop-adjusted mempool
denominator would be a future behavioral change, not the default v1 policy.

Fee estimate target keys are block counts. qbitd currently tracks estimates up
to 1008 blocks, so qbit-electrs keeps the standard target set
`1..25, 144, 504, 1008` while interpreting wall-clock time at qbit's 60-second
aggregate block target:

| Target blocks | Approximate qbit wall-clock |
| ---: | --- |
| 1 | 1 minute |
| 2 | 2 minutes |
| 6 | 6 minutes |
| 12 | 12 minutes |
| 25 | 25 minutes |
| 144 | 2.4 hours |
| 504 | 8.4 hours |
| 1008 | 16.8 hours |

Subsidy parameters:

| Network | Initial subsidy | Subsidy interval | Step multiplier |
| --- | ---: | ---: | --- |
| mainnet | `210 * COIN` | 43,200 blocks | `598 / 625` |
| testnet4 | `210 * COIN` | 43,200 blocks | `598 / 625` |
| regtest | `210 * COIN` | 150 blocks | `598 / 625` |

`COIN` is 100,000,000 sats. qbit applies the multiplier iteratively with floor
division at each step.

## P2MR

P2MR outputs use witness version 2 with a 32-byte Merkle-root witness program.
The scriptPubKey shape is:

```text
OP_2 <32-byte merkle-root program>
```

Address encoding uses Bech32m with the network HRP from the network table.
The Merkle root is the witness program; there is no internal key or taproot-style
tweak in the P2MR address shape.

Fixture generation should cover both normal P2MR witness spends and the qbit
script-size boundaries tracked by the fixture plan: 16 KiB per item and 128 KiB total
argument stack.

## qbit Opcodes

The qbit-specific opcode names from the qbit source snapshot are:

| Opcode | Byte |
| --- | --- |
| `OP_CHECKSIGPQC` | `0xb3` |
| `OP_CHECKTEMPLATEVERIFY` | `0xbb` |
| `OP_CHECKDATASIGPQC` | `0xbc` |
| `OP_CHECKDATASIGADDPQC` | `0xbd` |

The electrs REST `sigops` field mirrors qbit's legacy block-level sigop count.
P2MR script validation is bounded by per-input validation weight and contributes
zero to that legacy sigop counter; electrs does not expose a separate explorer
PQC operation count in the initial qbit port.

## AuxPoW Block Layout

qbit block parsing must treat the first 80 bytes as the pure block header. If
the header version signals AuxPoW, an AuxPoW payload follows the pure header and
precedes the transaction vector. `CBlock` serialization is therefore:

```text
pure 80-byte header
[AuxPoW payload if signaled]
transaction vector
```

The AuxPoW payload order in qbit is:

```text
coinbase transaction serialized without witness
coinbase merkle branch
coinbase branch index
chain merkle branch
chain index
parent block pure header
```

The AuxPoW version flag is `0x00000100`. qbit uses chain ID `31430` for the
mainnet placeholder, test chains, testnet4, and regtest at this source ref. The
parent block in the AuxPoW payload is a pure header, which avoids recursive
AuxPoW parsing.

## REST And Electrum Header APIs

qbit block identity is the hash of the pure 80-byte block header. qbit-electrs
therefore exposes pure 80-byte headers, not AuxPoW-extended serialized headers,
from REST and Electrum header endpoints:

- REST `/block/{hash}/header`
- Electrum `blockchain.headers.subscribe`
- Electrum `blockchain.block.header`
- Electrum `blockchain.block.headers`

AuxPoW payload bytes remain part of raw block serialization and parser fixtures,
but are intentionally absent from these header API responses. Header merkle-proof
helpers operate over pure-header hashes as well.

## Witness Archive Requirement

qbit-electrs fixtures and daemon ingestion require full witness history. qbitd's
`-prunewitnesses` mode is not acceptable for positive ingestion or parser
fixtures because historical witness data may be absent after the pruning depth.
Fixtures should preserve witness-inclusive serialized bytes and should mark any
negative witness-pruned samples explicitly as negative cases.
