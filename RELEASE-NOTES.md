# qbit-electrs release notes

qbit-electrs is based on mempool/electrs v3.3.0. qbit-specific release notes are
listed first; inherited upstream electrs notes are retained below for lineage.

## 3.3.0-qbit.1

### Added

* qbit mainnet, testnet4, and regtest network modes with qbit daemon defaults,
  Docker Compose support for an external `qbitd`, and packaging documentation.
* qbit wire-codec and daemon-ingestion support for P2MR addresses and scripts,
  WSF=1 fee and size semantics, pure 80-byte block headers, and AuxPoW block
  payload parsing.
* REST and Electrum behavior for qbit tip, header, address, UTXO, mempool,
  spend-confirmation, transaction-broadcast, fee-estimate, block, proof, and
  AuxPoW header flows.
* Local qbit regtest harness and read-only live qbit testnet4 canary flows that
  compare qbit RPC truth with REST, Electrum, AuxPoW, fee-estimate, mempool, and
  resource-metric outputs.
* Deterministic qbit fixture bundles for P2MR spends, AuxPoW blocks and headers,
  REST/Electrum snapshots, mempool vsize parity, fee estimates, mempool
  histograms, and sigop-adjusted vsize policy.

### Changed

* Retained inherited Bitcoin and Liquid electrs modes while scoping qbit
  correctness to the non-Liquid qbit networks.
* Standardized qbit fee-estimate targets around block counts while documenting
  qbit's shorter target block cadence.
* Kept qbit REST transaction `size`, `weight`, and `vsize` fields on
  witness-inclusive serialized byte length, with fixture evidence for qbitd's
  sigop-adjusted mempool vsize behavior.

### Validation

* Added release CI and harness source-fetch coverage for building qbit and
  qbit-electrs test inputs reproducibly.
* Expanded regression coverage across qbit codec boundaries, P2MR address and
  script behavior, REST/Electrum fixture snapshots, AuxPoW consumers, fee
  estimates, mempool metrics, and live-canary parity checks.

# Inherited electrs release notes

# 0.4.1 (14 Oct 2018)

* Don't run full compaction after initial import is over (when using JSONRPC)

# 0.4.0 (22 Sep 2018)

* Optimize for low-memory systems by using different RocksDB settings
* Rename `--skip_bulk_import` flag to `--jsonrpc-import`

# 0.3.2 (14 Sep 2018)

* Optimize block headers processing during startup
* Handle TCP disconnections during long RPCs
* Use # of CPUs for bulk indexing threads
* Update rust-bitcoin to 0.14
* Optimize block headers processing during startup


# 0.3.1 (20 Aug 2018)

* Reconnect to bitcoind only on transient errors
* Poll mempool after transaction broadcasting

# 0.3.0 (14 Aug 2018)

* Optimize for low-memory systems
* Improve compaction performance
* Handle disconnections from bitcoind by retrying
* Make `blk*.dat` ingestion more robust
* Support regtest network
* Support more Electrum RPC methods
* Export more Prometheus metrics (CPU, RAM, file descriptors)
* Add `scripts/run.sh` for building and running `electrs`
* Add some Python tools (as API usage examples)
* Change default Prometheus monitoring ports

# 0.2.0 (14 Jul 2018)

* Allow specifying custom bitcoind data directory
* Allow specifying JSONRPC cookie from commandline
* Improve initial bulk indexing performance
* Support 32-bit systems

# 0.1.0 (2 Jul 2018)

* Announcement: https://lists.linuxfoundation.org/pipermail/bitcoin-dev/2018-July/016190.html
* Published to https://crates.io/electrs and https://docs.rs/electrs
