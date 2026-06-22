#!/usr/bin/env python3
"""Export qbitd-generated P2MR boundary fixture bundles.

This exporter uses qbit's functional-test framework to capture the witness
policy edges that matter most for qbit-electrs size/fee handling:

- accepted P2MR spends at the 16 KiB stack-item and 128 KiB total initial-stack
  boundaries; and
- rejected P2MR spends just over each boundary.
"""

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
P2MR_LEAF_VERSION = 0xC0
MAX_STANDARD_P2MR_STACK_ITEM_SIZE = 16 * 1024
MAX_P2MR_V1_TOTAL_INITIAL_STACK_BYTES = 128 * 1024
ACCEPTED_SPEND_AMOUNT_SATS = 300_000
REJECTED_SPEND_AMOUNT_SATS = 300_000
INITIAL_MOCK_TIME = 1_900_100_000


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

from test_framework.blocktools import (  # noqa: E402
    COINBASE_MATURITY,
    MAX_STANDARD_TX_WEIGHT,
)
from test_framework.messages import (  # noqa: E402
    COutPoint,
    CTransaction,
    CTxIn,
    CTxInWitness,
    CTxOut,
)
from test_framework.script import (  # noqa: E402
    CScript,
    OP_2,
    OP_DROP,
    OP_DUP,
    OP_TRUE,
    TaggedHash,
    ser_string,
)
from test_framework.test_framework import BitcoinTestFramework  # noqa: E402
from test_framework.util import assert_equal  # noqa: E402
from test_framework.wallet import MiniWallet, MiniWalletMode  # noqa: E402


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


def p2mr_tapleaf_hash(script: CScript, leaf_version: int = P2MR_LEAF_VERSION) -> bytes:
    return TaggedHash("P2MRLeaf", bytes([leaf_version]) + ser_string(bytes(script)))


def p2mr_control_block(leaf_version: int = P2MR_LEAF_VERSION) -> bytes:
    return bytes([leaf_version | 1])


def p2mr_stack_items_for_total_bytes(total_bytes: int) -> list[bytes]:
    stack_items = []
    while total_bytes > 0:
        item_size = min(MAX_STANDARD_P2MR_STACK_ITEM_SIZE, total_bytes)
        stack_items.append(bytes([0x42]) * item_size)
        total_bytes -= item_size
    return stack_items


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


class ExportQbitP2mrBoundaryFixtures(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            "-asert",
            "-p2mronly=1",
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
            help="directory to receive the exported P2MR boundary fixture bundle",
        )

    def fund_p2mr_output(self, merkle_root: bytes, amount: int) -> dict:
        assert_equal(len(merkle_root), 32)
        script_pub_key = CScript([OP_2, merkle_root])
        funding = self.script_wallet.send_to(
            from_node=self.nodes[0],
            scriptPubKey=script_pub_key,
            amount=amount,
        )
        self.generate(self.script_wallet, 1)
        return {
            "txid": funding["txid"],
            "vout": funding["sent_vout"],
            "amount": amount,
        }

    def create_spend_tx(self, utxo: dict, witness_stack: list[bytes], fee: int) -> CTransaction:
        tx = CTransaction()
        tx.vin = [CTxIn(COutPoint(int(utxo["txid"], 16), utxo["vout"]))]
        tx.vout = [CTxOut(utxo["amount"] - fee, self.script_wallet.get_output_script())]
        tx.wit.vtxinwit = [CTxInWitness()]
        tx.wit.vtxinwit[0].scriptWitness.stack = witness_stack
        return tx

    def create_size_fee_spend_tx(
        self,
        utxo: dict,
        witness_stack: list[bytes],
    ) -> tuple[CTransaction, int]:
        tx = self.create_spend_tx(utxo, witness_stack, fee=1)
        fee = len(tx.serialize())
        if fee >= utxo["amount"]:
            raise AssertionError(f"fixture UTXO amount {utxo['amount']} cannot cover fee {fee}")
        tx = self.create_spend_tx(utxo, witness_stack, fee=fee)
        assert_equal(len(tx.serialize()), fee)
        return tx, fee

    def run_test(self):
        commit = qbit_source_commit()
        assert_equal(commit, QBIT_COMMIT)
        assert_equal(MAX_STANDARD_TX_WEIGHT, 400_000)

        output_dir = Path(self.options.output_dir).expanduser().resolve()
        node = self.nodes[0]
        node.setmocktime(INITIAL_MOCK_TIME)
        self.script_wallet = MiniWallet(node, mode=MiniWalletMode.ADDRESS_P2MR_OP_TRUE)
        self.generate(self.script_wallet, COINBASE_MATURITY + 1)

        command = (
            "python3 tests/fixtures/qbit/scripts/export_qbit_p2mr_boundary_fixtures.py "
            "--qbit-source .context/qbit-source --output-dir <output-dir> --randomseed=1"
        )
        source = {
            "kind": "qbitd-generated",
            "qbit_commit": commit,
            "source_ref": "qbit functional-test feature_p2mr.py boundary policy flows",
            "deterministic_key_material": (
                "qbit MiniWallet ADDRESS_P2MR_OP_TRUE, deterministic OP_DROP/OP_TRUE "
                "P2MR leaves, and fixed 0x42 stack bytes"
            ),
        }

        single_item = bytes([0x42]) * MAX_STANDARD_P2MR_STACK_ITEM_SIZE
        max_total_stack = p2mr_stack_items_for_total_bytes(
            MAX_P2MR_V1_TOTAL_INITIAL_STACK_BYTES
        )
        oversized_item = bytes([0x42]) * (MAX_STANDARD_P2MR_STACK_ITEM_SIZE + 1)
        oversized_total_stack = p2mr_stack_items_for_total_bytes(
            MAX_P2MR_V1_TOTAL_INITIAL_STACK_BYTES + 1
        )
        control_block = p2mr_control_block()
        cases = [
            {
                "key": "stack-16k",
                "accepted": True,
                "tx_id": "regtest-qbitd-p2mr-stack-16k-spend-tx",
                "tx_path": "transactions/regtest-qbitd-p2mr-stack-16k-spend.hex",
                "accept_id": "regtest-qbitd-testmempoolaccept-p2mr-stack-16k",
                "accept_path": "rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-16k.json",
                "mempool_id": "regtest-qbitd-getmempoolentry-p2mr-stack-16k",
                "mempool_path": "rpc/regtest-qbitd-getmempoolentry-p2mr-stack-16k.json",
                "amount": ACCEPTED_SPEND_AMOUNT_SATS,
                "stack_items": [single_item],
                "leaf_script": CScript([OP_DROP, OP_TRUE]),
                "notes": "Accepted qbitd-generated P2MR spend at the 16 KiB stack-item boundary.",
            },
            {
                "key": "stack-128k",
                "accepted": True,
                "tx_id": "regtest-qbitd-p2mr-stack-128k-spend-tx",
                "tx_path": "transactions/regtest-qbitd-p2mr-stack-128k-spend.hex",
                "accept_id": "regtest-qbitd-testmempoolaccept-p2mr-stack-128k",
                "accept_path": "rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-128k.json",
                "mempool_id": "regtest-qbitd-getmempoolentry-p2mr-stack-128k",
                "mempool_path": "rpc/regtest-qbitd-getmempoolentry-p2mr-stack-128k.json",
                "amount": ACCEPTED_SPEND_AMOUNT_SATS,
                "stack_items": max_total_stack,
                "leaf_script": CScript([OP_DROP] * len(max_total_stack) + [OP_TRUE]),
                "notes": (
                    "Accepted qbitd-generated P2MR spend at the 128 KiB total "
                    "initial-stack boundary."
                ),
            },
            {
                "key": "stack-item-oversize",
                "accepted": False,
                "tx_id": "regtest-qbitd-p2mr-stack-item-oversize-reject-tx",
                "tx_path": "transactions/regtest-qbitd-p2mr-stack-item-oversize-reject.hex",
                "accept_id": "regtest-qbitd-testmempoolaccept-p2mr-stack-item-oversize",
                "accept_path": "rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-item-oversize.json",
                "amount": REJECTED_SPEND_AMOUNT_SATS,
                "stack_items": [oversized_item],
                "leaf_script": CScript([OP_DUP, OP_DROP, OP_DROP, OP_TRUE]),
                "expected_reject_reason": "bad-witness-nonstandard",
                "notes": (
                    "Rejected qbitd-generated P2MR spend with one stack item one "
                    "byte above the 16 KiB standard-policy boundary."
                ),
            },
            {
                "key": "stack-total-oversize",
                "accepted": False,
                "tx_id": "regtest-qbitd-p2mr-stack-total-oversize-reject-tx",
                "tx_path": "transactions/regtest-qbitd-p2mr-stack-total-oversize-reject.hex",
                "accept_id": "regtest-qbitd-testmempoolaccept-p2mr-stack-total-oversize",
                "accept_path": "rpc/regtest-qbitd-testmempoolaccept-p2mr-stack-total-oversize.json",
                "amount": REJECTED_SPEND_AMOUNT_SATS,
                "stack_items": oversized_total_stack,
                "leaf_script": CScript([OP_DROP] * len(oversized_total_stack) + [OP_TRUE]),
                "expected_reject_reason": "bad-witness-nonstandard",
                "notes": (
                    "Rejected qbitd-generated P2MR spend with initial stack bytes one "
                    "byte above the 128 KiB standard-policy boundary."
                ),
            },
        ]

        for case in cases:
            case["utxo"] = self.fund_p2mr_output(
                p2mr_tapleaf_hash(case["leaf_script"]),
                amount=case["amount"],
            )

        accepted_cases = []
        for case in cases:
            witness_stack = case["stack_items"] + [
                bytes(case["leaf_script"]),
                control_block,
            ]
            tx, fee_sats = self.create_size_fee_spend_tx(case["utxo"], witness_stack)
            tx_hex = tx.serialize().hex()
            serialized_size = len(bytes.fromhex(tx_hex))
            stripped_size = len(tx.serialize_without_witness())
            bitcoin_wsf4_vsize = (3 * stripped_size + serialized_size + 3) // 4
            stack_lengths = [len(item) for item in case["stack_items"]]
            case["tx"] = tx
            case["tx_hex"] = tx_hex
            case["fee_sats"] = fee_sats
            case["txid"] = tx.txid_hex
            case["facts"] = {
                "serialized_size": serialized_size,
                "qbit_weight": tx.get_weight(),
                "qbit_vsize": tx.get_vsize(),
                "bitcoin_wsf4_vsize": bitcoin_wsf4_vsize,
                "witness_scale_factor": 1,
                "input_count": 1,
                "p2mr_witness": True,
                "initial_stack_item_count": len(case["stack_items"]),
                "initial_stack_item_lengths": stack_lengths,
                "initial_stack_total_bytes": sum(stack_lengths),
                "max_initial_stack_item_bytes": max(stack_lengths),
                "max_standard_stack_item_bytes": MAX_STANDARD_P2MR_STACK_ITEM_SIZE,
                "max_standard_total_initial_stack_bytes": (
                    MAX_P2MR_V1_TOTAL_INITIAL_STACK_BYTES
                ),
                "leaf_script_hex": bytes(case["leaf_script"]).hex(),
                "leaf_script_bytes": len(bytes(case["leaf_script"])),
                "control_block_hex": control_block.hex(),
                "control_block_bytes": len(control_block),
                "max_standard_tx_weight": MAX_STANDARD_TX_WEIGHT,
                "fee_sats": fee_sats,
                "spent_prevout_txid": case["utxo"]["txid"],
                "spent_prevout_vout": case["utxo"]["vout"],
            }

            accept = node.testmempoolaccept([tx_hex])[0]
            case["accept"] = accept
            if case["accepted"]:
                assert_equal(accept["allowed"], True)
                assert tx.get_weight() <= MAX_STANDARD_TX_WEIGHT
                txid = node.sendrawtransaction(tx_hex)
                assert_equal(txid, tx.txid_hex)
                case["mempool_entry"] = node.getmempoolentry(txid)
                accepted_cases.append(case)
            else:
                assert_equal(accept["allowed"], False)
                reason = case["expected_reject_reason"]
                if reason not in accept.get("reject-reason", ""):
                    raise AssertionError(f"unexpected reject reason for {case['key']}: {accept}")
                case["facts"]["expected_reject_reason"] = reason

        boundary_raw_mempool = node.getrawmempool()
        for case in accepted_cases:
            assert case["txid"] in boundary_raw_mempool

        mined_hash = self.generate(self.script_wallet, 1)[0]
        boundary_block_hex = node.getblock(mined_hash, 0)
        boundary_block_json = node.getblock(mined_hash, 1)
        boundary_block_header_json = node.getblockheader(mined_hash, True)
        for case in accepted_cases:
            assert case["txid"] in boundary_block_json["tx"]
            case["facts"]["confirmed_height"] = boundary_block_json["height"]

        block_size = len(bytes.fromhex(boundary_block_hex))
        contained_txids = [case["txid"] for case in accepted_cases]

        def tx_manifest_entry(case: dict) -> dict:
            return {
                "id": case["tx_id"],
                "network": "regtest",
                "fixture_type": "transaction",
                "path": case["tx_path"],
                "source": source,
                "generation_command": command,
                "txid": case["txid"],
                "expected_parser_facts": case["facts"],
                "refresh_policy": {
                    "reproduce": "Run this exporter with --randomseed=1 and review the manifest.",
                    "diff_policy": (
                        "Commit only reviewed qbitd output and keep the qbit "
                        "commit/source metadata."
                    ),
                },
                "notes": case["notes"],
            }

        def accept_manifest_entry(case: dict) -> dict:
            facts = {
                "method": "testmempoolaccept",
                "allowed": case["accepted"],
                "txid": case["txid"],
            }
            if case["accepted"]:
                facts["vsize_policy"] = (
                    "qbit WSF=1 witness-inclusive serialized size; no sigop "
                    "adjustment for this OP_DROP boundary fixture"
                )
                facts["fee_sats"] = case["fee_sats"]
            else:
                facts["reject_reason"] = case["expected_reject_reason"]
            return {
                "id": case["accept_id"],
                "network": "regtest",
                "fixture_type": "rpc",
                "path": case["accept_path"],
                "source": source,
                "generation_command": command,
                "txid": case["txid"],
                "expected_parser_facts": facts,
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_p2mr_boundary_fixtures.py.",
                    "diff_policy": "Review qbitd mempool-accept JSON shape changes before promotion.",
                },
                "notes": f"qbitd testmempoolaccept RPC truth for {case['key']}.",
            }

        def mempool_manifest_entry(case: dict) -> dict:
            return {
                "id": case["mempool_id"],
                "network": "regtest",
                "fixture_type": "rpc",
                "path": case["mempool_path"],
                "source": source,
                "generation_command": command,
                "txid": case["txid"],
                "expected_parser_facts": {
                    "method": "getmempoolentry",
                    "txid": case["txid"],
                    "vsize_policy": (
                        "qbit WSF=1 witness-inclusive serialized size; no sigop "
                        "adjustment for this OP_DROP boundary fixture"
                    ),
                    "fee_sats": case["fee_sats"],
                },
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_p2mr_boundary_fixtures.py.",
                    "diff_policy": "Review qbitd mempool JSON shape changes before promotion.",
                },
                "notes": f"qbitd mempool entry captured before {case['key']} is mined.",
            }

        fixtures = [tx_manifest_entry(case) for case in cases]
        fixtures.append(
            {
                "id": "regtest-qbitd-p2mr-boundary-spends-block",
                "network": "regtest",
                "fixture_type": "block",
                "path": "blocks/regtest-qbitd-p2mr-boundary-spends-block.hex",
                "source": source,
                "generation_command": command,
                "height": boundary_block_json["height"],
                "blockhash": mined_hash,
                "expected_parser_facts": {
                    "serialized_size": block_size,
                    "qbit_weight": block_size,
                    "witness_scale_factor": 1,
                    "pure_header_bytes": 80,
                    "auxpow": False,
                    "tx_count": len(boundary_block_json["tx"]),
                    "contains_txids": contained_txids,
                },
                "refresh_policy": {
                    "reproduce": "Regenerate with export_qbit_p2mr_boundary_fixtures.py.",
                    "diff_policy": "Review qbitd block and tx ordering changes before promotion.",
                },
                "notes": "qbitd block that confirms both accepted P2MR boundary spends.",
            }
        )
        fixtures.extend(accept_manifest_entry(case) for case in cases)
        fixtures.extend(mempool_manifest_entry(case) for case in accepted_cases)
        fixtures.extend(
            [
                {
                    "id": "regtest-qbitd-getrawmempool-with-p2mr-boundary-spends",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getrawmempool-with-p2mr-boundary-spends.json",
                    "source": source,
                    "generation_command": command,
                    "expected_parser_facts": {
                        "method": "getrawmempool",
                        "contains_txids": contained_txids,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_p2mr_boundary_fixtures.py.",
                        "diff_policy": "Review qbitd mempool JSON shape changes before promotion.",
                    },
                    "notes": "qbitd mempool txid snapshot for both accepted P2MR boundary spends.",
                },
                {
                    "id": "regtest-qbitd-getblock-p2mr-boundary-spends",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getblock-p2mr-boundary-spends.json",
                    "source": source,
                    "generation_command": command,
                    "height": boundary_block_json["height"],
                    "blockhash": mined_hash,
                    "expected_parser_facts": {
                        "method": "getblock",
                        "verbosity": 1,
                        "tx_count": len(boundary_block_json["tx"]),
                        "contains_txids": contained_txids,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_p2mr_boundary_fixtures.py.",
                        "diff_policy": "Review qbitd block JSON shape changes before promotion.",
                    },
                    "notes": "qbitd block RPC sample for the P2MR boundary-spend block.",
                },
                {
                    "id": "regtest-qbitd-getblockheader-p2mr-boundary-spends",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getblockheader-p2mr-boundary-spends.json",
                    "source": source,
                    "generation_command": command,
                    "height": boundary_block_json["height"],
                    "blockhash": mined_hash,
                    "expected_parser_facts": {
                        "method": "getblockheader",
                        "verbosity": True,
                        "header_bytes": 80,
                        "hash": mined_hash,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_p2mr_boundary_fixtures.py.",
                        "diff_policy": "Review qbitd header JSON shape changes before promotion.",
                    },
                    "notes": "qbitd header RPC sample for the P2MR boundary-spend block.",
                },
            ]
        )

        # Export manifests are relative to their output directory. Committed
        # manifest entries get prefixed when promoted into tests/fixtures/qbit.
        export_fixtures = json.loads(json.dumps(fixtures))
        for fixture in export_fixtures:
            fixture["path"] = fixture["path"].removeprefix("tests/fixtures/qbit/")

        manifest = {
            "schema_version": 1,
            "contract": {
                "document": "doc/qbit-contract.md",
                "qbit_repository": "qbit-reference",
                "qbit_commit": commit,
                "issues": [4, 12, 14, 20, 22],
            },
            "fixtures": export_fixtures,
        }

        for case in cases:
            write_text(output_dir, case["tx_path"], case["tx_hex"])
            write_json(output_dir, case["accept_path"], json_safe(case["accept"]))
            if case["accepted"]:
                write_json(
                    output_dir,
                    case["mempool_path"],
                    json_safe(case["mempool_entry"]),
                )
        write_text(
            output_dir,
            "blocks/regtest-qbitd-p2mr-boundary-spends-block.hex",
            boundary_block_hex,
        )
        write_json(
            output_dir,
            "rpc/regtest-qbitd-getrawmempool-with-p2mr-boundary-spends.json",
            json_safe(boundary_raw_mempool),
        )
        write_json(
            output_dir,
            "rpc/regtest-qbitd-getblock-p2mr-boundary-spends.json",
            json_safe(boundary_block_json),
        )
        write_json(
            output_dir,
            "rpc/regtest-qbitd-getblockheader-p2mr-boundary-spends.json",
            json_safe(boundary_block_header_json),
        )
        write_json(output_dir, "manifest.json", manifest)
        self.log.info("wrote qbit P2MR boundary fixture bundle to %s", output_dir)


if __name__ == "__main__":
    clamp_nofile()
    ExportQbitP2mrBoundaryFixtures(__file__).main()
