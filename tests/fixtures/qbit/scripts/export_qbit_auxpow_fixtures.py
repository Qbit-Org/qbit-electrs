#!/usr/bin/env python3
"""Export a reviewed qbitd-generated AuxPoW fixture bundle.

This script uses qbit's functional-test framework to create and submit one
regtest AuxPoW block, then writes raw block/header/RPC truth samples under the
requested output directory. It intentionally stays separate from
generate_qbit_fixtures.py because it requires qbitd and qbit's Python test
framework.
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
PURE_BLOCK_HEADER_LEN = 80
QBIT_AUXPOW_CHAIN_ID = 31430
INITIAL_MOCK_TIME = 1_900_000_000
AUXPOW_PARENT_TIME = INITIAL_MOCK_TIME + 600


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

from test_framework.auxpow import make_valid_auxpow_from_template  # noqa: E402
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


class ExportQbitAuxpowFixtures(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.extra_args = [[
            "-asert",
            "-p2mronly=1",
            "-auxpowtemplateexpiry=1",
            "-auxpowtemplatecachelimit=12",
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
            help="directory to receive the exported AuxPoW fixture bundle",
        )

    def run_test(self):
        commit = qbit_source_commit()
        assert_equal(commit, QBIT_COMMIT)

        output_dir = Path(self.options.output_dir).expanduser().resolve()
        node = self.nodes[0]
        node.setmocktime(INITIAL_MOCK_TIME)
        wallet = MiniWallet(node, mode=MiniWalletMode.ADDRESS_P2MR_OP_TRUE)
        self.generate(wallet, 101)

        node.setmocktime(AUXPOW_PARENT_TIME)

        payout_address = node._p2mr_mining_address()
        aux_template = node.createauxblock(payout_address)
        auxpow = make_valid_auxpow_from_template(
            aux_template,
            parent_time=AUXPOW_PARENT_TIME,
        )
        auxpow_hex = auxpow.to_hex()
        assert_equal(node.submitauxblock(aux_template["hash"], auxpow_hex), None)

        block_hash = aux_template["hash"]
        raw_block = node.getblock(block_hash, 0)
        block_json = node.getblock(block_hash, 1)
        header_json = node.getblockheader(block_hash, True)
        raw_extended_header = node.getblockheader(block_hash, False)
        raw_pure_header = raw_block[: PURE_BLOCK_HEADER_LEN * 2]

        if not raw_block.startswith(raw_extended_header):
            raise AssertionError("raw AuxPoW block should start with extended qbit header hex")
        if not raw_extended_header.startswith(raw_pure_header):
            raise AssertionError("extended AuxPoW header should start with pure qbit header hex")
        if len(raw_pure_header) != PURE_BLOCK_HEADER_LEN * 2:
            raise AssertionError(
                f"raw qbit pure header should be 80 bytes, got {len(raw_pure_header) // 2}"
            )
        if not raw_block.startswith(raw_pure_header):
            raise AssertionError("raw AuxPoW block should start with pure qbit header hex")

        block_size = len(bytes.fromhex(raw_block))
        extended_header_bytes = len(bytes.fromhex(raw_extended_header))
        tx_count = len(block_json["tx"])
        manifest = {
            "schema_version": 1,
            "contract": {
                "document": "doc/qbit-contract.md",
                "qbit_repository": "qbit-reference",
                "qbit_commit": commit,
                "issues": [12, 13, 14, 20, 22],
            },
            "fixtures": [
                {
                    "id": "regtest-qbitd-auxpow-block",
                    "network": "regtest",
                    "fixture_type": "block",
                    "path": "blocks/regtest-qbitd-auxpow-block.hex",
                    "source": {
                        "kind": "qbitd-generated",
                        "qbit_commit": commit,
                        "source_ref": "qbit functional-test createauxblock/submitauxblock flow",
                        "deterministic_key_material": (
                            "qbit MiniWallet ADDRESS_P2MR_OP_TRUE and deterministic "
                            "test-node P2MR mining address"
                        ),
                    },
                    "generation_command": (
                        "ulimit -n 1024 && python3 "
                        "tests/fixtures/qbit/scripts/export_qbit_auxpow_fixtures.py "
                        "--qbit-source .context/qbit-source --output-dir <output-dir> --randomseed=1"
                    ),
                    "height": aux_template["height"],
                    "blockhash": block_hash,
                    "expected_parser_facts": {
                        "serialized_size": block_size,
                        "qbit_weight": block_size,
                        "witness_scale_factor": 1,
                        "pure_header_bytes": PURE_BLOCK_HEADER_LEN,
                        "auxpow": True,
                        "auxpow_chain_id": QBIT_AUXPOW_CHAIN_ID,
                        "auxpow_extended_header_bytes": extended_header_bytes,
                        "tx_count": tx_count,
                        "header_hex": raw_pure_header,
                        "extended_header_hex": raw_extended_header,
                    },
                    "refresh_policy": {
                        "reproduce": "Run this exporter with --randomseed=1 and review the manifest.",
                        "diff_policy": (
                            "Commit only reviewed qbitd output and keep the qbit "
                            "commit/source metadata."
                        ),
                    },
                    "notes": "qbitd-generated AuxPoW block captured after submitauxblock.",
                },
                {
                    "id": "regtest-qbitd-auxpow-pure-header",
                    "network": "regtest",
                    "fixture_type": "metadata",
                    "path": "headers/regtest-qbitd-auxpow-header.hex",
                    "source": {
                        "kind": "qbitd-generated",
                        "qbit_commit": commit,
                        "source_ref": "first 80 bytes of promoted qbitd AuxPoW block",
                    },
                    "generation_command": (
                        "ulimit -n 1024 && python3 "
                        "tests/fixtures/qbit/scripts/export_qbit_auxpow_fixtures.py "
                        "--qbit-source .context/qbit-source --output-dir <output-dir> --randomseed=1"
                    ),
                    "height": aux_template["height"],
                    "blockhash": block_hash,
                    "expected_parser_facts": {
                        "header_bytes": PURE_BLOCK_HEADER_LEN,
                        "header_contract": "pure qbit identity header",
                        "auxpow": True,
                        "hash": block_hash,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_auxpow_fixtures.py.",
                        "diff_policy": "Review header contract changes before promotion.",
                    },
                    "notes": "Pure qbit block identity header for the promoted AuxPoW block.",
                },
                {
                    "id": "regtest-qbitd-auxpow-extended-header",
                    "network": "regtest",
                    "fixture_type": "metadata",
                    "path": "headers/regtest-qbitd-auxpow-extended-header.hex",
                    "source": {
                        "kind": "qbitd-generated",
                        "qbit_commit": commit,
                        "source_ref": "qbit getblockheader false for the promoted AuxPoW block",
                    },
                    "generation_command": (
                        "ulimit -n 1024 && python3 "
                        "tests/fixtures/qbit/scripts/export_qbit_auxpow_fixtures.py "
                        "--qbit-source .context/qbit-source --output-dir <output-dir> --randomseed=1"
                    ),
                    "height": aux_template["height"],
                    "blockhash": block_hash,
                    "expected_parser_facts": {
                        "header_bytes": extended_header_bytes,
                        "header_contract": "qbitd AuxPoW-extended serialized header",
                        "starts_with_pure_header": True,
                        "auxpow": True,
                        "hash": block_hash,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_auxpow_fixtures.py.",
                        "diff_policy": "Review qbitd serialized-header changes before promotion.",
                    },
                    "notes": "qbitd returns this AuxPoW-extended header for getblockheader false.",
                },
                {
                    "id": "regtest-qbitd-auxpow-payload",
                    "network": "regtest",
                    "fixture_type": "metadata",
                    "path": "auxpow/regtest-qbitd-auxpow-payload.hex",
                    "source": {
                        "kind": "qbitd-generated",
                        "qbit_commit": commit,
                        "source_ref": "qbit functional-test make_valid_auxpow_from_template payload",
                    },
                    "generation_command": (
                        "ulimit -n 1024 && python3 "
                        "tests/fixtures/qbit/scripts/export_qbit_auxpow_fixtures.py "
                        "--qbit-source .context/qbit-source --output-dir <output-dir> --randomseed=1"
                    ),
                    "height": aux_template["height"],
                    "blockhash": block_hash,
                    "expected_parser_facts": {
                        "serialized_size": len(bytes.fromhex(auxpow_hex)),
                        "payload_order": [
                            "coinbase_tx_no_witness",
                            "coinbase_merkle_branch",
                            "coinbase_branch_index",
                            "chain_merkle_branch",
                            "chain_index",
                            "parent_block_pure_header",
                        ],
                        "auxpow_chain_id": QBIT_AUXPOW_CHAIN_ID,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_auxpow_fixtures.py.",
                        "diff_policy": "Review qbit AuxPoW payload serialization changes before promotion.",
                    },
                    "notes": "AuxPoW payload submitted to qbitd for the promoted block.",
                },
                {
                    "id": "regtest-qbitd-getblock-auxpow",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getblock-auxpow.json",
                    "source": {
                        "kind": "qbitd-generated",
                        "qbit_commit": commit,
                        "source_ref": "qbit getblock RPC after submitauxblock",
                    },
                    "generation_command": "Regenerate with export_qbit_auxpow_fixtures.py.",
                    "height": aux_template["height"],
                    "blockhash": block_hash,
                    "expected_parser_facts": {
                        "method": "getblock",
                        "verbosity": 1,
                        "tx_count": tx_count,
                        "auxpow": True,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_auxpow_fixtures.py.",
                        "diff_policy": "Review qbitd RPC JSON shape changes before promotion.",
                    },
                    "notes": "qbitd getblock RPC sample for the AuxPoW block.",
                },
                {
                    "id": "regtest-qbitd-getblockheader-auxpow",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getblockheader-auxpow.json",
                    "source": {
                        "kind": "qbitd-generated",
                        "qbit_commit": commit,
                        "source_ref": "qbit getblockheader RPC after submitauxblock",
                    },
                    "generation_command": "Regenerate with export_qbit_auxpow_fixtures.py.",
                    "height": aux_template["height"],
                    "blockhash": block_hash,
                    "expected_parser_facts": {
                        "method": "getblockheader",
                        "verbosity": True,
                        "verbose_json_has_no_serialized_header": True,
                        "pure_header_fixture": "regtest-qbitd-auxpow-pure-header",
                        "extended_header_fixture": "regtest-qbitd-auxpow-extended-header",
                        "hash": block_hash,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_auxpow_fixtures.py.",
                        "diff_policy": "Review qbitd RPC JSON shape changes before promotion.",
                    },
                    "notes": "qbitd getblockheader RPC sample; raw header fixture is pure 80 bytes.",
                },
                {
                    "id": "regtest-qbitd-createauxblock-template",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-createauxblock-template.json",
                    "source": {
                        "kind": "qbitd-generated",
                        "qbit_commit": commit,
                        "source_ref": "qbit createauxblock RPC before submitauxblock",
                    },
                    "generation_command": "Regenerate with export_qbit_auxpow_fixtures.py.",
                    "height": aux_template["height"],
                    "blockhash": block_hash,
                    "expected_parser_facts": {
                        "method": "createauxblock",
                        "chainid": QBIT_AUXPOW_CHAIN_ID,
                        "hash": block_hash,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with export_qbit_auxpow_fixtures.py.",
                        "diff_policy": "Review qbitd AuxPoW RPC shape changes before promotion.",
                    },
                    "notes": "qbitd createauxblock template used for the promoted AuxPoW block.",
                },
            ],
        }

        write_text(output_dir, "blocks/regtest-qbitd-auxpow-block.hex", raw_block)
        write_text(output_dir, "headers/regtest-qbitd-auxpow-header.hex", raw_pure_header)
        write_text(
            output_dir,
            "headers/regtest-qbitd-auxpow-extended-header.hex",
            raw_extended_header,
        )
        write_text(output_dir, "auxpow/regtest-qbitd-auxpow-payload.hex", auxpow_hex)
        write_json(output_dir, "rpc/regtest-qbitd-getblock-auxpow.json", json_safe(block_json))
        write_json(
            output_dir,
            "rpc/regtest-qbitd-getblockheader-auxpow.json",
            json_safe(header_json),
        )
        write_json(
            output_dir,
            "rpc/regtest-qbitd-createauxblock-template.json",
            json_safe(aux_template),
        )
        write_json(output_dir, "manifest.json", manifest)
        self.log.info("wrote qbit AuxPoW fixture bundle to %s", output_dir)


if __name__ == "__main__":
    clamp_nofile()
    ExportQbitAuxpowFixtures(__file__).main()
