#!/usr/bin/env python3
"""Generate deterministic qbit fixture files.

This script is intentionally self-contained so fixture checks do not need a
live qbitd. The Bech32m algorithm mirrors qbit's pinned
test/functional/test_framework/segwit_addr.py and src/key_io.cpp behavior for
witness version 2 P2MR addresses.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

QBIT_COMMIT = "57bb53575f0d4931e77ac4a34b7e7f4c049f0636"
P2MR_ZERO_PROGRAM_HEX = "00" * 32
P2MR_ZERO_SCRIPT_HEX = "5220" + P2MR_ZERO_PROGRAM_HEX

WITNESS_TX_HEX = (
    "020000000001010000000000000000000000000000000000000000000000000000000000000000"
    "ffffffff00ffffffff010000000000000000000102abcd00000000"
)
NON_AUXPOW_BLOCK_HEX = "010000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001020000000001010000000000000000000000000000000000000000000000000000000000000000ffffffff00ffffffff010000000000000000000102abcd00000000"
AUXPOW_BLOCK_HEX = "00c1582f0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001000000010000000000000000000000000000000000000000000000000000000000000000ffffffff00ffffffff010000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001020000000001010000000000000000000000000000000000000000000000000000000000000000ffffffff00ffffffff010000000000000000000102abcd00000000"

NETWORKS = {
    "mainnet": {"hrp": "qb", "wrong_networks": ["testnet4", "regtest"]},
    "testnet4": {"hrp": "tq", "wrong_networks": ["mainnet", "regtest"]},
    "regtest": {"hrp": "qbrt", "wrong_networks": ["mainnet", "testnet4"]},
}

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32M_CONST = 0x2BC830A3


def bech32_polymod(values):
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= generator[i]
    return chk


def bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_create_checksum(hrp, data):
    values = bech32_hrp_expand(hrp) + data
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ BECH32M_CONST
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32m_encode(hrp, data):
    combined = data + bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise ValueError("invalid convertbits input")
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("invalid unpadded convertbits input")
    return ret


def p2mr_address(network):
    program = bytes.fromhex(P2MR_ZERO_PROGRAM_HEX)
    data = [2] + convertbits(program, 8, 5)
    return bech32m_encode(NETWORKS[network]["hrp"], data)


def json_bytes(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def address_fixture(network):
    wrong_networks = [
        {"network": other, "address": p2mr_address(other)}
        for other in NETWORKS[network]["wrong_networks"]
    ]
    return {
        "schema_version": 1,
        "fixture_type": "address",
        "network": network,
        "qbit_commit": QBIT_COMMIT,
        "source": {
            "kind": "derived",
            "source_ref": "qbit src/key_io.cpp WitnessV2P2MR and test_framework/segwit_addr.py",
        },
        "p2mr": {
            "witness_version": 2,
            "witness_program_hex": P2MR_ZERO_PROGRAM_HEX,
            "script_pubkey_hex": P2MR_ZERO_SCRIPT_HEX,
            "bech32_encoding": "bech32m",
            "hrp": NETWORKS[network]["hrp"],
            "address": p2mr_address(network),
        },
        "wrong_network_rejection_vectors": wrong_networks,
    }


def db_row_fixture():
    return {
        "schema_version": 1,
        "fixture_type": "db-row",
        "network": "regtest",
        "qbit_commit": QBIT_COMMIT,
        "source": {
            "kind": "derived",
            "source_ref": "electrs non-liquid TxHistoryRow schema with qbit P2MR script",
        },
        "script_pubkey_hex": P2MR_ZERO_SCRIPT_HEX,
        "script_hash_hex": hashlib.sha256(bytes.fromhex(P2MR_ZERO_SCRIPT_HEX)).hexdigest(),
        "history_row": {
            "kind": "funding",
            "confirmed_height": 2,
            "tx_position": 3,
            "txid": "02" * 32,
            "vout": 3,
            "value_sat": 7,
        },
    }


def generated_files():
    files = {
        "transactions/regtest-witness-tx.hex": (WITNESS_TX_HEX + "\n").encode(),
        "blocks/regtest-synthetic-non-auxpow-block.hex": (NON_AUXPOW_BLOCK_HEX + "\n").encode(),
        "blocks/regtest-synthetic-auxpow-block.hex": (AUXPOW_BLOCK_HEX + "\n").encode(),
        "db-rows/regtest-p2mr-zero-program-history.json": json_bytes(db_row_fixture()),
    }
    for network in NETWORKS:
        files[f"addresses/{network}-p2mr-zero-program.json"] = json_bytes(address_fixture(network))
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="qbit fixture root",
    )
    parser.add_argument("--write", action="store_true", help="write generated fixtures")
    parser.add_argument("--check", action="store_true", help="verify generated fixtures")
    args = parser.parse_args()
    check = args.check or not args.write
    root = args.root

    mismatches = []
    for relpath, data in generated_files().items():
        path = root / relpath
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if check:
            actual = path.read_bytes() if path.exists() else None
            if actual != data:
                mismatches.append(relpath)

    if mismatches:
        print("Fixture regeneration mismatch:", file=sys.stderr)
        for relpath in mismatches:
            print(f"  {relpath}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
