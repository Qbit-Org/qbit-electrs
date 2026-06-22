#!/usr/bin/env python3
"""Export qbitd-generated sigop-adjusted mempool vsize fixtures."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import resource
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_QBIT_SOURCE = ROOT / ".context" / "qbit-source"
QBIT_COMMIT = "57bb53575f0d4931e77ac4a34b7e7f4c049f0636"
DEFAULT_BYTES_PER_SIGOP = 20
NUM_SIGOPS = 222
FUND_AMOUNT_SATS = 1_000_000
INITIAL_MOCK_TIME = 1_900_120_000


def preparse_qbit_source() -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--qbit-source",
        default=os.environ.get("QBIT_SOURCE_DIR", str(DEFAULT_QBIT_SOURCE)),
    )
    args, _ = parser.parse_known_args()
    return Path(args.qbit_source).expanduser().resolve()


QBIT_SOURCE = preparse_qbit_source()
QBIT_CONFIG = QBIT_SOURCE / "build" / "test" / "config.ini"
if not any(arg == "--configfile" or arg.startswith("--configfile=") for arg in sys.argv):
    sys.argv.extend(["--configfile", str(QBIT_CONFIG)])

sys.path.insert(0, str(QBIT_SOURCE / "test" / "functional"))

from test_framework.blocktools import COINBASE_MATURITY  # noqa: E402
from test_framework.messages import (  # noqa: E402
    COIN,
    COutPoint,
    CTransaction,
    CTxIn,
    CTxInWitness,
    CTxOut,
    WITNESS_SCALE_FACTOR,
)
from test_framework.script import (  # noqa: E402
    CScript,
    OP_CHECKMULTISIG,
    OP_CHECKSIG,
    OP_ENDIF,
    OP_FALSE,
    OP_IF,
    OP_TRUE,
)
from test_framework.script_util import script_to_p2wsh_script  # noqa: E402
from test_framework.test_framework import BitcoinTestFramework  # noqa: E402
from test_framework.util import assert_equal, assert_greater_than  # noqa: E402
from test_framework.wallet import MiniWallet  # noqa: E402


def clamp_nofile() -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return
    if soft != resource.RLIM_INFINITY:
        return
    finite_hard = hard if hard != resource.RLIM_INFINITY else 1024
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(1024, finite_hard), hard))


def qbit_source_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=QBIT_SOURCE,
        text=True,
    ).strip()


def write_text(root: Path, relpath: str, text: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n")


def write_json(root: Path, relpath: str, value: object) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: json_safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [json_safe(child) for child in value]
    return value


class ExportQbitSigopVsizeFixtures(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            "-asert",
            "-fallbackfee=0.0001",
        ]]

    def add_options(self, parser):
        parser.add_argument(
            "--qbit-source",
            default=str(QBIT_SOURCE),
            help="qbit source checkout matching doc/qbit-contract.md",
        )
        parser.add_argument(
            "--output-dir",
            required=True,
            help="directory to receive the exported sigop-vsize fixture bundle",
        )

    @staticmethod
    def witness_script() -> CScript:
        num_multisigops = NUM_SIGOPS // 20
        num_singlesigops = NUM_SIGOPS % 20
        return CScript(
            [OP_FALSE, OP_IF]
            + [OP_CHECKMULTISIG] * num_multisigops
            + [OP_CHECKSIG] * num_singlesigops
            + [OP_ENDIF, OP_TRUE]
        )

    def create_spend_tx(self, funding: dict, fee_sats: int) -> CTransaction:
        tx = CTransaction()
        tx.vin = [CTxIn(COutPoint(int(funding["txid"], 16), funding["sent_vout"]))]
        tx.vout = [
            CTxOut(FUND_AMOUNT_SATS - fee_sats, self.wallet.get_output_script()),
        ]
        tx.wit.vtxinwit = [CTxInWitness()]
        tx.wit.vtxinwit[0].scriptWitness.stack = [bytes(self.witness_script())]
        return tx

    def run_test(self):
        commit = qbit_source_commit()
        assert_equal(commit, QBIT_COMMIT)
        assert_equal(WITNESS_SCALE_FACTOR, 1)

        output_dir = Path(self.options.output_dir).expanduser().resolve()
        node = self.nodes[0]
        node.setmocktime(INITIAL_MOCK_TIME)
        self.wallet = MiniWallet(node)
        self.generate(self.wallet, COINBASE_MATURITY + 1)

        witness_script = self.witness_script()
        funding = self.wallet.send_to(
            from_node=node,
            scriptPubKey=script_to_p2wsh_script(witness_script),
            amount=FUND_AMOUNT_SATS,
        )
        self.generate(self.wallet, 1)

        sigop_equivalent_vsize = (
            NUM_SIGOPS * DEFAULT_BYTES_PER_SIGOP + WITNESS_SCALE_FACTOR - 1
        ) // WITNESS_SCALE_FACTOR
        tx = self.create_spend_tx(funding, fee_sats=sigop_equivalent_vsize)
        tx_hex = tx.serialize().hex()
        serialized_size = len(bytes.fromhex(tx_hex))
        assert_greater_than(sigop_equivalent_vsize, serialized_size)

        acceptance = node.testmempoolaccept([tx_hex])[0]
        assert_equal(acceptance["allowed"], True)
        assert_equal(acceptance["vsize"], sigop_equivalent_vsize)

        txid = node.sendrawtransaction(tx_hex)
        assert_equal(txid, tx.txid_hex)
        raw_verbose = node.getrawtransaction(txid, True)
        mempool_entry = node.getmempoolentry(txid)
        raw_mempool = node.getrawmempool()
        assert txid in raw_mempool
        assert_equal(mempool_entry["vsize"], sigop_equivalent_vsize)
        assert_equal(mempool_entry["weight"], serialized_size)
        assert_equal(mempool_entry["ancestorsize"], sigop_equivalent_vsize)
        assert_equal(mempool_entry["descendantsize"], sigop_equivalent_vsize)

        base_fee_sats = round(float(mempool_entry["fees"]["base"]) * COIN)
        assert_equal(base_fee_sats, sigop_equivalent_vsize)

        tx_path = "transactions/regtest-qbitd-p2wsh-sigop-adjusted-vsize-spend.hex"
        accept_path = "rpc/regtest-qbitd-testmempoolaccept-p2wsh-sigop-adjusted-vsize.json"
        mempool_path = "rpc/regtest-qbitd-getmempoolentry-p2wsh-sigop-adjusted-vsize.json"
        raw_tx_path = "rpc/regtest-qbitd-getrawtransaction-p2wsh-sigop-adjusted-vsize.json"
        raw_mempool_path = "rpc/regtest-qbitd-getrawmempool-with-sigop-adjusted-vsize.json"
        command = (
            "python3 tests/fixtures/qbit/scripts/export_qbit_sigop_vsize_fixtures.py "
            "--qbit-source .context/qbit-source --output-dir <output-dir> --randomseed=1"
        )
        source = {
            "kind": "qbitd-generated",
            "qbit_commit": commit,
            "source_ref": "qbit functional-test mempool_sigoplimit.py accepted bytespersigop flow",
            "deterministic_key_material": (
                "qbit MiniWallet deterministic P2WSH funding, fixed sigop script, "
                "and fixed mocktime"
            ),
        }
        facts = {
            "serialized_size": serialized_size,
            "qbit_weight": serialized_size,
            "qbit_tx_vsize": serialized_size,
            "witness_scale_factor": WITNESS_SCALE_FACTOR,
            "script_type": "p2wsh",
            "p2mr_witness": False,
            "sigop_cost": NUM_SIGOPS,
            "bytes_per_sigop": DEFAULT_BYTES_PER_SIGOP,
            "sigop_adjusted_vsize": sigop_equivalent_vsize,
            "mempool_vsize": mempool_entry["vsize"],
            "mempool_vsize_exceeds_serialized_size": True,
            "fee_sats": sigop_equivalent_vsize,
            "spent_prevout_txid": funding["txid"],
            "spent_prevout_vout": funding["sent_vout"],
            "witness_script_hex": bytes(witness_script).hex(),
        }

        fixtures = [
            {
                "id": "regtest-qbitd-p2wsh-sigop-adjusted-vsize-spend-tx",
                "network": "regtest",
                "fixture_type": "transaction",
                "path": tx_path,
                "source": source,
                "generation_command": command,
                "txid": txid,
                "expected_parser_facts": facts,
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_sigop_vsize_fixtures.py.",
                    "diff_policy": "Review qbitd tx bytes and sigop-vsize policy changes before promotion.",
                },
                "notes": (
                    "qbitd-generated P2WSH spend whose mempool vsize is inflated "
                    "by bytes-per-sigop policy above its serialized byte size."
                ),
            },
            {
                "id": "regtest-qbitd-testmempoolaccept-p2wsh-sigop-adjusted-vsize",
                "network": "regtest",
                "fixture_type": "rpc",
                "path": accept_path,
                "source": source,
                "generation_command": command,
                "txid": txid,
                "expected_parser_facts": {
                    "method": "testmempoolaccept",
                    "allowed": True,
                    "txid": txid,
                    "vsize_policy": "qbit bytespersigop-adjusted mempool vsize",
                    "sigop_adjusted_vsize": sigop_equivalent_vsize,
                    "serialized_size": serialized_size,
                },
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_sigop_vsize_fixtures.py.",
                    "diff_policy": "Review qbitd mempool-accept JSON shape changes before promotion.",
                },
                "notes": "qbitd testmempoolaccept RPC truth for sigop-adjusted mempool vsize.",
            },
            {
                "id": "regtest-qbitd-getmempoolentry-p2wsh-sigop-adjusted-vsize",
                "network": "regtest",
                "fixture_type": "rpc",
                "path": mempool_path,
                "source": source,
                "generation_command": command,
                "txid": txid,
                "expected_parser_facts": {
                    "method": "getmempoolentry",
                    "txid": txid,
                    "vsize_policy": "qbit bytespersigop-adjusted mempool vsize",
                    "sigop_adjusted_vsize": sigop_equivalent_vsize,
                    "serialized_size": serialized_size,
                    "fee_sats": sigop_equivalent_vsize,
                },
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_sigop_vsize_fixtures.py.",
                    "diff_policy": "Review qbitd mempool JSON shape changes before promotion.",
                },
                "notes": "qbitd mempool entry captured before the sigop-adjusted spend is mined.",
            },
            {
                "id": "regtest-qbitd-getrawtransaction-p2wsh-sigop-adjusted-vsize",
                "network": "regtest",
                "fixture_type": "rpc",
                "path": raw_tx_path,
                "source": source,
                "generation_command": command,
                "txid": txid,
                "expected_parser_facts": {
                    "method": "getrawtransaction",
                    "verbosity": True,
                    "txid": txid,
                    "vsize_policy": "qbit tx JSON serialized size under WSF=1",
                    "serialized_size": serialized_size,
                },
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_sigop_vsize_fixtures.py.",
                    "diff_policy": "Review qbitd raw transaction JSON shape changes before promotion.",
                },
                "notes": "qbitd verbose transaction RPC for the sigop-adjusted mempool-vsize sample.",
            },
            {
                "id": "regtest-qbitd-getrawmempool-with-sigop-adjusted-vsize",
                "network": "regtest",
                "fixture_type": "rpc",
                "path": raw_mempool_path,
                "source": source,
                "generation_command": command,
                "txid": txid,
                "expected_parser_facts": {
                    "method": "getrawmempool",
                    "contains_txid": txid,
                },
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_sigop_vsize_fixtures.py.",
                    "diff_policy": "Review qbitd mempool JSON shape changes before promotion.",
                },
                "notes": "qbitd mempool txid snapshot for the sigop-adjusted-vsize sample.",
            },
        ]

        manifest = {
            "schema_version": 1,
            "contract": {
                "document": "doc/qbit-contract.md",
                "qbit_repository": "qbit-reference",
                "qbit_commit": commit,
                "issues": [4, 12, 14, 20, 22],
            },
            "fixtures": fixtures,
        }

        write_text(output_dir, tx_path, tx_hex)
        write_json(output_dir, accept_path, json_safe(acceptance))
        write_json(output_dir, mempool_path, json_safe(mempool_entry))
        write_json(output_dir, raw_tx_path, json_safe(raw_verbose))
        write_json(output_dir, raw_mempool_path, json_safe(raw_mempool))
        write_json(output_dir, "manifest.json", manifest)
        self.log.info("wrote qbit sigop-vsize fixture bundle to %s", output_dir)


if __name__ == "__main__":
    clamp_nofile()
    ExportQbitSigopVsizeFixtures(__file__).main()
