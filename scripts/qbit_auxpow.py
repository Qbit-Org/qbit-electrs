#!/usr/bin/env python3
"""Small qbit AuxPoW payload builder for qbit-electrs tests.

This mirrors the qbit functional-test helper shape used by createauxblock /
submitauxblock, but keeps the qbit-electrs harness independent of a qbit source
checkout when QBITD and QBIT_CLI point at release binaries.
"""

from __future__ import annotations

import hashlib


BLOCK_VERSION_TOP_BITS = 0x20000000
QBIT_AUXPOW_CHAIN_ID = 31430
MERGED_MINING_HEADER = bytes.fromhex("fabe6d6d")
LCG_MULTIPLIER = 1103515245
LCG_INCREMENT = 12345
UINT32_MASK = 0xFFFFFFFF
OP_TRUE = 0x51


def hash256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def ser_compact_size(size: int) -> bytes:
    if size < 0:
        raise ValueError("negative compact size")
    if size < 253:
        return size.to_bytes(1, "little")
    if size <= 0xFFFF:
        return b"\xfd" + size.to_bytes(2, "little")
    if size <= 0xFFFF_FFFF:
        return b"\xfe" + size.to_bytes(4, "little")
    return b"\xff" + size.to_bytes(8, "little")


def ser_string(data: bytes) -> bytes:
    return ser_compact_size(len(data)) + data


def ser_vector(items: list[bytes]) -> bytes:
    return ser_compact_size(len(items)) + b"".join(items)


def ser_uint256(value: int) -> bytes:
    return value.to_bytes(32, "little")


def ser_uint256_vector(items: list[int]) -> bytes:
    return ser_vector([ser_uint256(item) for item in items])


def uint256_from_str(data: bytes) -> int:
    return int.from_bytes(data[:32], "little")


def uint256_from_compact(compact: int) -> int:
    size = compact >> 24
    word = compact & 0x007F_FFFF
    if size <= 3:
        return word >> (8 * (3 - size))
    return word << (8 * (size - 3))


def script_push(data: bytes) -> bytes:
    size = len(data)
    if size < 0x4C:
        return bytes([size]) + data
    if size <= 0xFF:
        return b"\x4c" + bytes([size]) + data
    if size <= 0xFFFF:
        return b"\x4d" + size.to_bytes(2, "little") + data
    return b"\x4e" + size.to_bytes(4, "little") + data


def advance_slot_lcg(value: int) -> int:
    return (value * LCG_MULTIPLIER + LCG_INCREMENT) & UINT32_MASK


def expected_chain_index(*, nonce: int, chain_id: int, merkle_height: int) -> int:
    rand = advance_slot_lcg(nonce)
    rand = (rand + chain_id) & UINT32_MASK
    rand = advance_slot_lcg(rand)
    if merkle_height >= 32:
        return rand
    return rand & ((1 << merkle_height) - 1)


def merkle_branch_root(*, leaf: int, branch: list[int], index: int) -> int:
    merkle_hash = leaf
    for sibling in branch:
        if index & 1:
            merkle_hash = uint256_from_str(hash256(ser_uint256(sibling) + ser_uint256(merkle_hash)))
        else:
            merkle_hash = uint256_from_str(hash256(ser_uint256(merkle_hash) + ser_uint256(sibling)))
        index >>= 1
    return merkle_hash


def txin(prev_hash: int, prev_n: int, script_sig: bytes, sequence: int) -> bytes:
    return (
        ser_uint256(prev_hash)
        + prev_n.to_bytes(4, "little")
        + ser_string(script_sig)
        + sequence.to_bytes(4, "little")
    )


def txout(value: int, script_pubkey: bytes) -> bytes:
    return value.to_bytes(8, "little", signed=True) + ser_string(script_pubkey)


def coinbase_tx(commitment: bytes) -> bytes:
    return (
        (1).to_bytes(4, "little", signed=True)
        + ser_vector([txin(0, 0xFFFF_FFFF, script_push(commitment), 0)])
        + ser_vector([txout(0, bytes([OP_TRUE]))])
        + (0).to_bytes(4, "little")
    )


def block_header(*, merkle_root: int, ntime: int, nbits: int, nonce: int) -> bytes:
    return (
        (1).to_bytes(4, "little", signed=True)
        + ser_uint256(0)
        + ser_uint256(merkle_root)
        + ntime.to_bytes(4, "little")
        + nbits.to_bytes(4, "little")
        + nonce.to_bytes(4, "little")
    )


def solved_parent_header(*, merkle_root: int, ntime: int, nbits: int) -> bytes:
    target = uint256_from_compact(nbits)
    nonce = 0
    while uint256_from_str(hash256(block_header(merkle_root=merkle_root, ntime=ntime, nbits=nbits, nonce=nonce))) > target:
        nonce += 1
    return block_header(merkle_root=merkle_root, ntime=ntime, nbits=nbits, nonce=nonce)


def make_valid_auxpow_from_template(
    template: dict,
    *,
    parent_time: int = 0,
    nonce: int = 0,
    expected_chain_id: int | None = None,
) -> str:
    chain_id = int(template["chainid"])
    if expected_chain_id is not None and chain_id != expected_chain_id:
        raise ValueError(f"unexpected qbit AuxPoW chain id: {chain_id}")

    chain_merkle_branch: list[int] = []
    coinbase_merkle_branch: list[int] = []
    chain_index = expected_chain_index(
        nonce=nonce,
        chain_id=chain_id,
        merkle_height=len(chain_merkle_branch),
    )
    chain_root = merkle_branch_root(
        leaf=int(template["hash"], 16),
        branch=chain_merkle_branch,
        index=chain_index,
    )
    commitment = (
        MERGED_MINING_HEADER
        + ser_uint256(chain_root)
        + (1 << len(chain_merkle_branch)).to_bytes(4, "little")
        + nonce.to_bytes(4, "little")
    )

    parent_coinbase = coinbase_tx(commitment)
    parent_merkle_root = merkle_branch_root(
        leaf=uint256_from_str(hash256(parent_coinbase)),
        branch=coinbase_merkle_branch,
        index=0,
    )
    parent_block = solved_parent_header(
        merkle_root=parent_merkle_root,
        ntime=parent_time,
        nbits=int(template["bits"], 16),
    )

    payload = (
        parent_coinbase
        + ser_uint256_vector(coinbase_merkle_branch)
        + (0).to_bytes(4, "little", signed=True)
        + ser_uint256_vector(chain_merkle_branch)
        + chain_index.to_bytes(4, "little", signed=True)
        + parent_block
    )
    return payload.hex()
