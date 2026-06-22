# qbit-electrs

qbit-electrs is a qbit-aware Electrum and REST indexer forked from
[mempool/electrs](https://github.com/mempool/electrs) v3.3.0, which is based on
[romanz/electrs](https://github.com/romanz/electrs) and
[Blockstream/electrs](https://github.com/Blockstream/electrs).

The fork preserves the upstream mempool/electrs API shape and lineage while
adding qbit network support, serialization, P2MR, AuxPoW, and runtime behavior.

Upstream REST API documentation [is available here](https://mempool.space/docs/api/rest).

Documentation for the database schema and indexing process [is available here](doc/schema.md).

The canonical qbit constants and serialization contract are tracked in
[the qbit contract](doc/qbit-contract.md).

### Installing & indexing

Install Rust, a compatible qbit daemon, and the `clang` and `cmake` packages
used by RocksDB. For qbit, run `qbitd` unpruned and point qbit-electrs at it:

```bash
$ git clone <qbit-electrs repository URL> qbit-electrs
$ cd qbit-electrs
$ cargo run --release --bin electrs -- \
    -vvvv \
    --network qbittestnet4 \
    --daemon-dir ~/.qbit/testnet4 \
    --daemon-rpc-addr 127.0.0.1:48352 \
    --jsonrpc-import \
    --cookie "$QBIT_RPC_COOKIE"

# Inherited Bitcoin and Liquid modes remain available:
$ cargo run --release --bin electrs -- -vvvv --network mainnet --daemon-dir ~/.bitcoin
$ cargo run --features liquid --release --bin electrs -- -vvvv --network liquid --daemon-dir ~/.liquid
```

See [electrs's original documentation](https://github.com/romanz/electrs/blob/master/doc/usage.md) for more detailed instructions.
Note that our indexes are incompatible with electrs's and has to be created separately.

### qbit external daemon runtime

qbit-electrs expects an already-running, unpruned `qbitd`; the Docker setup does
not bundle or start qbitd. Defaults match [the qbit contract](doc/qbit-contract.md):

| qbit network | electrs `--network` | qbit RPC | qbit data directory |
| --- | --- | --- | --- |
| mainnet | `qbit` | `8352` | `~/.qbit` |
| testnet4 | `qbittestnet4` | `48352` | `~/.qbit/testnet4` |
| regtest | `qbitregtest` | `18452` | `~/.qbit/regtest` |

Local testnet4 example:

```bash
cargo run --release --bin electrs -- \
  -vvvv \
  --network qbittestnet4 \
  --daemon-dir ~/.qbit/testnet4 \
  --daemon-rpc-addr 127.0.0.1:48352 \
  --jsonrpc-import \
  --cookie "$QBIT_RPC_COOKIE"
```

Docker Compose defaults to qbit testnet4 and connects to qbitd on the host:

```bash
QBIT_RPC_COOKIE='user:password' docker compose up --build
```

Use `QBIT_DAEMON_RPC_ADDR`, `QBIT_DAEMON_DIR`, `ELECTRS_NETWORK`,
`ELECTRS_HTTP_PORT`, `ELECTRS_ELECTRUM_PORT`, and `ELECTRS_MONITORING_PORT` to
point the same compose file at qbit mainnet or regtest. Do not commit RPC
credentials.

For read-only live qbit testnet4 validation, use
[`scripts/qbit-testnet4-canary.py`](scripts/qbit-testnet4-canary.py). It starts
qbit-electrs against an existing unpruned qbit testnet4 RPC endpoint and records
tip, block, AuxPoW, REST, and Electrum parity artifacts. See
[`doc/usage.md`](doc/usage.md#live-qbit-testnet4-canary) for configuration
examples.

The indexes require 1.3TB of storage after running compaction (as of October 2023), but you'll need to have
free space of about double that available during the index compaction process.
Creating the indexes should take a few hours on a beefy machine with high speed NVMe SSD(s).

### Light mode

For personal or low-volume use, you may set `--lightmode` to reduce disk storage requirements
by roughly 50% at the cost of slower and more expensive lookups.

With this option set, raw transactions and metadata associated with blocks will not be kept in rocksdb
(the `T`, `X` and `M` indexes),
but instead queried from the backing daemon on demand.

### Notable qbit features

- qbit mainnet, testnet4, and regtest network modes with qbit-specific default
  RPC, HTTP, Electrum, and monitoring ports.
- qbit serialization for P2MR scripts and addresses, WSF=1 transaction sizing,
  pure 80-byte block headers, and AuxPoW payload parsing with normalized header
  responses.
- qbit REST and Electrum behavior for block and proof lookups, transaction
  broadcast, address and UTXO queries, fee estimates, and mempool views.
- Deterministic qbit fixtures plus regtest and live testnet4 canaries covering
  P2MR, AuxPoW, fee estimates, mempool histograms, sigop-adjusted vsize, and
  REST/Electrum/qbit RPC parity.

### Notable changes from Electrs

- HTTP REST API in addition to the Electrum JSON-RPC protocol, with extended transaction information
  (previous outputs, spending transactions, script asm and more).

- Extended indexes and database storage for improved performance under high load:

  - A full transaction store mapping txids to raw transactions is kept in the database under the prefix `t`.
  - An index of all spendable transaction outputs is kept under the prefix `O`.
  - An index of all addresses (encoded as string) is kept under the prefix `a` to enable by-prefix address search.
  - A map of blockhash to txids is kept in the database under the prefix `X`.
  - Block stats metadata (number of transactions, size and weight) is kept in the database under the prefix `M`.

  With these new indexes, the backing daemon is no longer queried to serve user requests and is only polled
  periodically for new blocks and for syncing the mempool.

- Support for Liquid and other Elements-based networks, including CT, peg-in/out and multi-asset.
  (requires enabling the `liquid` feature flag using `--features liquid`)

### CLI options

In addition to electrs's original configuration options, a few new options are also available:

- `--http-addr <addr:port>` - HTTP server address/port to listen on (qbit defaults: `3000` mainnet, `3004` testnet4, `3002` regtest).
- `--lightmode` - enable light mode (see above)
- `--cors <origins>` - origins allowed to make cross-site request (optional, defaults to none).
- `--address-search` - enables the by-prefix address search index.
- `--index-unspendables` - enables indexing of provably unspendable outputs.
- `--utxos-limit <num>` - maximum number of utxos to return per address.
- `--electrum-txs-limit <num>` - maximum number of txs to return per address in the electrum server (does not apply for the http api).
- `--electrum-banner <text>` - welcome banner text for electrum server.

Additional options with the `liquid` feature (retained for upstream
compatibility; not part of qbit v1 correctness):
- `--parent-network <network>` - the parent network this chain is pegged to.

Additional options with the `electrum-discovery` feature:
- `--electrum-public-hosts <json>` - a json map of the public hosts where the electrum server is reachable, in the [`server.features` format](https://electrumx.readthedocs.io/en/latest/protocol-methods.html#server.features). Ignored for qbit networks in v1.
- `--electrum-announce` - announce the electrum server on the electrum p2p server discovery network. Ignored for qbit networks in v1.

See `$ cargo run --bin electrs -- --help` for the full list of options.

## License

MIT
