# qbit-electrs usage

qbit-electrs is a qbit-aware fork of mempool/electrs v3.3.0. See
[`doc/qbit-contract.md`](qbit-contract.md) for the canonical qbit network
constants and serialization contract.

## Installation

Install [latest Rust](https://rustup.rs/) (1.31+), a compatible qbit daemon for
qbit indexing, and optionally
[latest Bitcoin Core](https://bitcoincore.org/en/download/) (0.16+) for the
inherited Bitcoin mode,
and [latest Electrum wallet](https://electrum.org/#download) (3.2+).

Also, install the following packages (on Debian):
```bash
$ sudo apt update
$ sudo apt install clang cmake  # for building 'rust-rocksdb'
```

## Build

First build should take ~20 minutes:
```bash
$ cargo build --release
```

## qbit daemon configuration

Run qbitd unpruned before starting qbit-electrs. qbit defaults are:

| qbit network | electrs `--network` | qbit RPC | qbit data directory |
| --- | --- | --- | --- |
| mainnet | `qbit` | `8352` | `~/.qbit` |
| testnet4 | `qbittestnet4` | `48352` | `~/.qbit/testnet4` |
| regtest | `qbitregtest` | `18452` | `~/.qbit/regtest` |

Example qbit testnet4 run:

```bash
$ cargo run --release --bin electrs -- \
    -vvvv \
    --network qbittestnet4 \
    --daemon-dir ~/.qbit/testnet4 \
    --daemon-rpc-addr 127.0.0.1:48352 \
    --jsonrpc-import \
    --cookie "$QBIT_RPC_COOKIE"
```

## Local qbit regtest harness

The smoke harness starts an isolated qbit regtest node from the
[`doc/qbit-contract.md`](qbit-contract.md) source ref, mines mature P2MR
coinbase outputs, syncs qbit-electrs, and smoke-checks REST/Electrum tip,
header, address, UTXO, empty-mempool, P2MR spend, mempool, and spend-confirmation
probes against qbitd. It verifies REST address and scripthash parity for the
same P2MR script, Electrum P2MR scripthash subscribe/history/balance/listunspent,
broadcast, fee-method responses, REST `/fee-estimates`, and qbit pure-header
behavior for `blockchain.headers.subscribe`, `blockchain.block.header`, and
`blockchain.block.headers` with checkpoint proofs. It also checks qbit WSF=1
REST tx size fields, REST block and proof endpoints, and one indexed AuxPoW
block's pure-header/raw-block contract. It invalidates and reconsiders the
spend-confirming block to check local reorg handling. Live testnet scenarios
remain follow-up #21 coverage.

```bash
$ ./scripts/qbit-regtest-harness.py --qbit-source auto --build-qbit --build-electrs
```

Useful overrides:

- `QBIT_SOURCE_DIR`, or `--qbit-source`, points at a compatible qbit
  checkout. The source checkout is needed only when building qbit locally; the
  harness has an electrs-local AuxPoW payload builder for prebuilt qbit
  binaries.
- `QBIT_SOURCE_REPO_URL`, or `--qbit-repo`, overrides the default public
  `Qbit-Org/qbit` source repository when `--qbit-source auto` needs to clone a
  qbit source checkout.
- `QBITD` and `QBIT_CLI` point at prebuilt binaries and skip building qbit from
  the source checkout.
- `ELECTRS_BIN` points at a prebuilt electrs binary.
- `QBIT_HARNESS_KEEP=1` or `--keep-artifacts` keeps logs and the generated
  manifest under `target/qbit-regtest-harness/`.
- `--export-qbit-fixtures` keeps the successful artifact directory and writes a
  `qbitd-fixtures/` bundle with qbitd-generated P2MR transaction hex, block hex,
  RPC JSON samples, and a schema-compatible fixture manifest for review.

## Live qbit testnet4 canary

The canary script runs read-only checks against an existing qbit testnet4
archive node. It
starts a local qbit-electrs process with `--jsonrpc-import` and a temporary DB,
then compares qbit RPC truth with electrs REST and Electrum responses. It uses
only read-only qbit RPC methods such as `getblockchaininfo`, `getnetworkinfo`,
`getblockhash`, `getblockheader`, `getblock`, `getrawtransaction`,
`getmempoolinfo`, `getarchivepeers`, and `validateaddress`.

The backing qbit node must be unpruned and must expose full block and witness
data. A node with block pruning or witness pruning is not suitable for
qbit-electrs validation. The canary fails early if qbit reports `pruned=true`,
advertises `WITNESS_PRUNED`, or does not advertise `ARCHIVE`.

Example against a local or SSH-forwarded qbit RPC endpoint:

```bash
$ QBIT_RPC_COOKIE_FILE=/path/to/qbit-rpc-cookie \
  ./scripts/qbit-testnet4-canary.py \
    --node fermion-testnet4 \
    --qbit-rpc 127.0.0.1:48352 \
    --build-electrs \
    --duration 300 \
    --interval 60 \
    --known-height 17000 \
    --keep-artifacts
```

For tailnet nodes where RPC is bound to remote loopback, forward the RPC port
over SSH first:

```bash
$ ssh -N -L 48352:127.0.0.1:48352 fermion-testnet4
$ ssh -N -L 48353:127.0.0.1:48352 boson-testnet4
```

Then run the canary once per node, changing `--node` and `--qbit-rpc`:

```bash
$ QBIT_RPC_COOKIE_FILE=/path/to/qbit-rpc-cookie \
  ./scripts/qbit-testnet4-canary.py \
    --node boson-testnet4 \
    --qbit-rpc 127.0.0.1:48353 \
    --build-electrs \
    --duration 300 \
    --interval 60 \
    --known-height 17000 \
    --keep-artifacts
```

Artifacts are written under `target/qbit-testnet4-canary/`. The manifest records
the qbit/electrs tips, checked block heights, AuxPoW scan result, fixture
manifest reference, Electrum checks, REST-vs-qbit mempool parity, selected
Prometheus/resource metrics, REST/Electrum fee-estimate parity, and failure
context including log excerpts. Use `QBIT_RPC_COOKIE` or
`QBIT_RPC_COOKIE_FILE`; avoid putting RPC credentials in shell history. This
Use the saved artifacts to review live-node parity, fixture provenance, and
large-witness stress evidence before promoting canary evidence.

## Inherited Bitcoin daemon configuration

Allow Bitcoin Core to sync before starting qbit-electrs in inherited Bitcoin
mode:
```bash
$ bitcoind -server=1 -txindex=0 -prune=0
```

If you are using `-rpcuser=USER` and `-rpcpassword=PASSWORD` for authentication, please use `--cookie="USER:PASSWORD"` command-line flag.
Otherwise, [`~/.bitcoin/.cookie`](https://github.com/bitcoin/bitcoin/blob/0212187fc624ea4a02fc99bc57ebd413499a9ee1/contrib/debian/examples/bitcoin.conf#L70-L72) will be read, allowing this server to use bitcoind JSONRPC interface.

## Usage

First index sync should take ~1.5 hours:
```bash
$ cargo run --release -- -vvv --timestamp --db-dir ./db [--cookie="USER:PASSWORD"]
2018-08-17T18:27:42 - INFO - NetworkInfo { version: 179900, subversion: "/Satoshi:0.17.99/" }
2018-08-17T18:27:42 - INFO - BlockchainInfo { chain: "main", blocks: 537204, headers: 537204, bestblockhash: "0000000000000000002956768ca9421a8ddf4e53b1d81e429bd0125a383e3636", pruned: false, initialblockdownload: false }
2018-08-17T18:27:42 - DEBUG - opening DB at "./db/mainnet"
2018-08-17T18:27:42 - DEBUG - full compaction marker: None
2018-08-17T18:27:42 - INFO - listing block files at "/home/user/.bitcoin/blocks/blk*.dat"
2018-08-17T18:27:42 - INFO - indexing 1348 blk*.dat files
2018-08-17T18:27:42 - DEBUG - found 0 indexed blocks
2018-08-17T18:27:55 - DEBUG - applying 537205 new headers from height 0
2018-08-17T19:31:01 - DEBUG - no more blocks to index
2018-08-17T19:31:03 - DEBUG - no more blocks to index
2018-08-17T19:31:03 - DEBUG - last indexed block: best=0000000000000000002956768ca9421a8ddf4e53b1d81e429bd0125a383e3636 height=537204 @ 2018-08-17T15:24:02Z
2018-08-17T19:31:05 - DEBUG - opening DB at "./db/mainnet"
2018-08-17T19:31:06 - INFO - starting full compaction
2018-08-17T19:58:19 - INFO - finished full compaction
2018-08-17T19:58:19 - INFO - enabling auto-compactions
2018-08-17T19:58:19 - DEBUG - opening DB at "./db/mainnet"
2018-08-17T19:58:26 - DEBUG - applying 537205 new headers from height 0
2018-08-17T19:58:27 - DEBUG - downloading new block headers (537205 already indexed) from 000000000000000000150d26fcc38b8c3b71ae074028d1d50949ef5aa429da00
2018-08-17T19:58:27 - INFO - best=000000000000000000150d26fcc38b8c3b71ae074028d1d50949ef5aa429da00 height=537218 @ 2018-08-17T16:57:50Z (14 left to index)
2018-08-17T19:58:28 - DEBUG - applying 14 new headers from height 537205
2018-08-17T19:58:29 - INFO - RPC server running on 127.0.0.1:50001
```

The index database is stored here:
```bash
$ du db/
38G db/mainnet/
```

## Electrum client
```bash
# Connect only to the local server, for better privacy
$ ./scripts/local-electrum.bash
+ ADDR=127.0.0.1
+ PORT=50001
+ PROTOCOL=t
+ electrum --oneserver --server=127.0.0.1:50001:t
<snip>
```

In order to use a secure connection, TLS-terminating proxy (e.g. [hitch](https://github.com/varnish/hitch)) is recommended:
```bash
$ hitch --backend=[127.0.0.1]:50001 --frontent=[127.0.0.1]:50002 pem_file
$ electrum --oneserver --server=127.0.0.1:50002:s
```

## Docker
```bash
$ docker build -t qbit-electrs .
$ docker run --network host \
             --volume /home/roman/.bitcoin:/home/user/.bitcoin:ro \
             --volume $PWD:/home/user \
             --rm -i -t qbit-electrs
```

### qbit with an external qbitd

The qbit container path runs only qbit-electrs. Start and manage `qbitd`
separately with pruning disabled, then point qbit-electrs at that daemon.

Default qbit RPC ports and daemon data directories are:

| qbit network | electrs `--network` | qbit RPC | qbit data directory |
| --- | --- | --- | --- |
| mainnet | `qbit` | `8352` | `~/.qbit` |
| testnet4 | `qbittestnet4` | `48352` | `~/.qbit/testnet4` |
| regtest | `qbitregtest` | `18452` | `~/.qbit/regtest` |

Run against a host qbit testnet4 daemon:

```bash
$ QBIT_RPC_COOKIE='user:password' docker compose up --build
```

The compose file defaults to:

```bash
--network qbittestnet4
--daemon-rpc-addr host.docker.internal:48352
--jsonrpc-import
--http-addr 0.0.0.0:3004
--electrum-rpc-addr 0.0.0.0:40001
--monitoring-addr 0.0.0.0:44224
```

For qbit regtest, override the network, daemon RPC address, and exposed electrs
ports:

```bash
$ ELECTRS_NETWORK=qbitregtest \
  QBIT_DAEMON_RPC_ADDR=host.docker.internal:18452 \
  QBIT_DAEMON_DIR=/qbit/regtest \
  ELECTRS_HTTP_PORT=3002 \
  ELECTRS_ELECTRUM_PORT=60401 \
  ELECTRS_MONITORING_PORT=24224 \
  QBIT_RPC_COOKIE='user:password' \
  docker compose up --build
```

## Monitoring

Indexing and serving metrics are exported via [Prometheus](https://github.com/pingcap/rust-prometheus):

```bash
$ sudo apt install prometheus
$ echo "
scrape_configs:
  - job_name: electrs
    static_configs:
    - targets: ['localhost:4224']
" | sudo tee -a /etc/prometheus/prometheus.yml
$ sudo systemctl restart prometheus
$ firefox 'http://localhost:9090/graph?g0.range_input=1h&g0.expr=index_height&g0.tab=0'
```
