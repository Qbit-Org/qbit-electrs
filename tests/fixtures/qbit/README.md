# qbit Fixtures

This directory is the fixture root for qbit parser, address, RPC, block, and
transaction samples. The authoritative qbit contract is
[`doc/qbit-contract.md`](../../../doc/qbit-contract.md); do not duplicate the
network constant table in fixture metadata.

The fixture plan comes from:

- `#12`: canonical qbit parameter freeze
- `#14`: deterministic fixture generation
- `#22`: execution order

## Layout

```text
tests/fixtures/qbit/
├── README.md
├── manifest.schema.json
├── manifest.example.json
├── qbitd-export-manifest.example.json
├── addresses/
├── auxpow/
├── blocks/
├── db-rows/
├── headers/
├── transactions/
├── rpc/
└── scripts/
```

`scripts/` contains deterministic refresh helpers. Real fixture files are
committed under the matching data directory once generated.

## Refreshing

Check that committed deterministic fixtures are current:

```sh
python3 tests/fixtures/qbit/scripts/generate_qbit_fixtures.py --check
```

Regenerate the script-owned fixtures:

```sh
python3 tests/fixtures/qbit/scripts/generate_qbit_fixtures.py --write --check
```

The generator is intentionally offline and does not require a running qbitd. It
uses the pinned qbit Bech32m/P2MR contract for derived address fixtures and
replays the committed synthetic parser bytes from the manifest.

## qbitd-Generated Export Bundles

The local regtest harness can also export real qbitd-generated P2MR transaction,
block, and RPC samples into its artifact directory:

```sh
./scripts/qbit-regtest-harness.py --qbit-source auto --build-qbit --build-electrs --export-qbit-fixtures
```

`--export-qbit-fixtures` keeps the successful harness artifact directory and
writes `qbitd-fixtures/manifest.json` plus `transactions/`, `blocks/`, and
`rpc/` files. The export manifest follows `manifest.schema.json`; see
`qbitd-export-manifest.example.json` for the contract shape. These generated
bytes are not committed automatically because qbitd wallet keys, txids, and
block hashes may vary. Promote selected files into this fixture tree only after
reviewing the export manifest and preserving its qbit commit, generation
command, height, txid/blockhash, expected parser facts, and refresh policy.

`manifest.json` includes one promoted qbitd-generated P2MR spend bundle:

```text
transactions/regtest-qbitd-p2mr-spend.hex
blocks/regtest-qbitd-p2mr-spend-block.hex
rpc/regtest-qbitd-getrawtransaction-p2mr-spend.json
rpc/regtest-qbitd-getblock-p2mr-spend.json
rpc/regtest-qbitd-getblockheader-p2mr-spend.json
rpc/regtest-qbitd-getmempoolentry-p2mr-spend.json
rpc/regtest-qbitd-getrawmempool-with-p2mr-spend.json
```

This bundle is a reviewed snapshot, not output owned by the offline generator.
Verify its internal consistency with:

```sh
cargo test --test qbit_promoted_fixtures
```

`manifest.json` also includes a promoted qbitd-generated P2MR boundary bundle
for the 16 KiB stack-item and 128 KiB total initial-stack policy edges:

```text
transactions/regtest-qbitd-p2mr-stack-16k-spend.hex
transactions/regtest-qbitd-p2mr-stack-128k-spend.hex
transactions/regtest-qbitd-p2mr-stack-item-oversize-reject.hex
transactions/regtest-qbitd-p2mr-stack-total-oversize-reject.hex
blocks/regtest-qbitd-p2mr-boundary-spends-block.hex
rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-16k.json
rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-128k.json
rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-item-oversize.json
rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-total-oversize.json
rpc/regtest-qbitd-getmempoolentry-p2mr-stack-16k.json
rpc/regtest-qbitd-getmempoolentry-p2mr-stack-128k.json
rpc/regtest-qbitd-getrawmempool-with-p2mr-boundary-spends.json
rpc/regtest-qbitd-getblock-p2mr-boundary-spends.json
rpc/regtest-qbitd-getblockheader-p2mr-boundary-spends.json
```

Regenerate the source bundle with:

```sh
python3 tests/fixtures/qbit/scripts/export_qbit_p2mr_boundary_fixtures.py \
  --qbit-source .context/qbit-source \
  --output-dir target/qbit-p2mr-boundary-fixtures/manual \
  --randomseed=1
```

The accepted boundary transactions intentionally pay `fee_sats ==
serialized_size`, giving a 1 sat/qbit-vbyte qbitd mempool truth sample with no
Bitcoin witness discount.

## qbitd-Generated Sigop-Adjusted Vsize Bundle

`manifest.json` also includes a promoted qbitd-generated P2WSH spend that
exercises qbit's mempool `bytespersigop` virtual-size adjustment:

```text
transactions/regtest-qbitd-p2wsh-sigop-adjusted-vsize-spend.hex
rpc/regtest-qbitd-testmempoolaccept-p2wsh-sigop-adjusted-vsize.json
rpc/regtest-qbitd-getmempoolentry-p2wsh-sigop-adjusted-vsize.json
rpc/regtest-qbitd-getrawtransaction-p2wsh-sigop-adjusted-vsize.json
rpc/regtest-qbitd-getrawmempool-with-sigop-adjusted-vsize.json
```

Regenerate the source bundle with:

```sh
python3 tests/fixtures/qbit/scripts/export_qbit_sigop_vsize_fixtures.py \
  --qbit-source .context/qbit-source \
  --output-dir target/qbit-sigop-vsize-fixtures/manual \
  --randomseed=1
```

This bundle intentionally is not P2MR. It mirrors qbit's own
`mempool_sigoplimit.py` policy coverage and records a transaction whose qbit tx
JSON `size`, `weight`, and `vsize` are 115 bytes while qbitd mempool RPC reports
`vsize == 4440` from the `bytespersigop` policy path. qbit-electrs v1 keeps
REST tx fields, `/mempool` backlog, and `mempool.get_fee_histogram` on the
serialized-byte denominator; matching qbitd's sigop-adjusted mempool denominator
would be a separate behavior change.

## qbitd-Generated AuxPoW Bundle

`manifest.json` also includes a promoted qbitd-generated AuxPoW block bundle:

```text
blocks/regtest-qbitd-auxpow-block.hex
headers/regtest-qbitd-auxpow-header.hex
headers/regtest-qbitd-auxpow-extended-header.hex
auxpow/regtest-qbitd-auxpow-payload.hex
rpc/regtest-qbitd-createauxblock-template.json
rpc/regtest-qbitd-getblock-auxpow.json
rpc/regtest-qbitd-getblockheader-auxpow.json
```

Regenerate the source bundle with:

```sh
python3 tests/fixtures/qbit/scripts/export_qbit_auxpow_fixtures.py \
  --qbit-source .context/qbit-source \
  --output-dir target/qbit-auxpow-fixtures/manual \
  --randomseed=1
```

On macOS, if qbitd reports an infinite-descriptor limit as unavailable, prefix
the command with `ulimit -n 1024 &&`. The exporter uses qbit's functional-test
`createauxblock` / `submitauxblock` helpers, clamps the descriptor limit when it
can, and sets fixed mocktime before mining so byte output is reproducible.

The pure header fixture is the first 80 bytes of the raw AuxPoW block. The
extended-header fixture is qbitd's `getblockheader false` output for the same
block and includes the AuxPoW payload before the transaction vector.

## Manifest Provenance

Every committed fixture should have a manifest entry that records:

- qbit commit used to generate or verify the fixture
- network (`mainnet`, `testnet4`, or `regtest`)
- fixture type (`address`, `block`, `transaction`, `rpc`, `script`, or another
  schema-approved type)
- generation command
- txid, blockhash, address, or script where applicable
- expected parser facts
- source kind: `qbitd-generated`, `synthetic`, or `derived`
- refresh policy

If a fixture was generated from an older qbit commit than the manifest contract
commit, the top-level `compatible_fixture_commits` list must name the commit,
the exact fixture ids, and why the relevant semantics are unchanged. Do not
rewrite qbitd-generated fixture commit metadata without regenerating and
reviewing the bytes/RPC truth from that qbit source.

Prefer qbit functional-test helpers and regtest generation over handwritten
bytes. Synthetic fixtures are allowed only when the manifest explains why qbitd
cannot be the source of truth for that sample.

Refreshes must reproduce the previous bytes exactly. If a refresh produces a
diff, the manifest should document whether it is an intentional fixture update
or requires a qbit source-ref update in `doc/qbit-contract.md`.

## Live Canary Artifacts

`scripts/qbit-testnet4-canary.py` can produce live testnet4 evidence under
`target/qbit-testnet4-canary/`. Those artifacts are useful for live-canary review, but
they are not committed fixtures by default. Promote a live RPC/block/transaction
sample into this directory only after adding manifest provenance for #14:
qbit commit, node/network, height or txid, generation command, expected parser
facts, and refresh policy.
