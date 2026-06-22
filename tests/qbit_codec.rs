use bitcoin::consensus::encode::serialize;

use electrs::qbit_codec::{
    deserialize_block, deserialize_header, deserialize_pure_header, deserialize_transaction,
    has_auxpow_flag, transaction_size, transaction_weight, Error, AUXPOW_VERSION_FLAG,
    PURE_BLOCK_HEADER_LEN, WITNESS_SCALE_FACTOR,
};

fn fixture_hex(path: &str) -> Vec<u8> {
    let hex = std::fs::read_to_string(path).expect("fixture hex should be readable");
    hex::decode(hex.split_whitespace().collect::<String>()).expect("fixture hex should decode")
}

#[test]
fn qbit_auxpow_version_requires_bip9_top_bits() {
    assert!(!has_auxpow_flag(0x0000_0100));
    assert!(has_auxpow_flag(0x2000_0100));
    assert!(has_auxpow_flag(0x2f58_c100));
}

#[test]
fn qbit_witness_transaction_round_trips_and_uses_wsf_1_weight() {
    let raw = fixture_hex("tests/fixtures/qbit/transactions/regtest-witness-tx.hex");
    let tx = deserialize_transaction(&raw).expect("witness transaction should parse");

    assert_eq!(serialize(&tx), raw);
    assert_eq!(tx.input.len(), 1);
    assert_eq!(tx.output.len(), 1);
    assert_eq!(tx.input[0].witness.len(), 1);
    assert_eq!(tx.input[0].witness.iter().next().unwrap(), &[0xab, 0xcd]);

    assert_eq!(WITNESS_SCALE_FACTOR, 1);
    assert_eq!(transaction_size(&tx), raw.len());
    assert_eq!(transaction_weight(&tx), raw.len());
}

#[test]
fn qbit_block_header_boundary_is_exactly_80_bytes() {
    let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-synthetic-non-auxpow-block.hex");

    let header = deserialize_pure_header(&raw[..PURE_BLOCK_HEADER_LEN])
        .expect("first 80 bytes should be the pure qbit header");
    assert!(!has_auxpow_flag(header.version));

    assert_eq!(
        deserialize_pure_header(&raw[..PURE_BLOCK_HEADER_LEN - 1]),
        Err(Error::HeaderLength {
            actual: PURE_BLOCK_HEADER_LEN - 1
        })
    );
    assert_eq!(
        deserialize_pure_header(&raw[..PURE_BLOCK_HEADER_LEN + 1]),
        Err(Error::HeaderLength {
            actual: PURE_BLOCK_HEADER_LEN + 1
        })
    );
}

#[test]
fn qbit_header_parser_normalizes_pure_and_auxpow_headers() {
    let non_auxpow =
        fixture_hex("tests/fixtures/qbit/blocks/regtest-synthetic-non-auxpow-block.hex");
    let auxpow_extended_header =
        fixture_hex("tests/fixtures/qbit/headers/regtest-qbitd-auxpow-extended-header.hex");

    let non_auxpow_header = deserialize_header(&non_auxpow[..PURE_BLOCK_HEADER_LEN])
        .expect("pure qbit header should parse");
    assert_eq!(
        non_auxpow_header.block_hash().to_string(),
        "4ddd9f0855d58a375be5a763e5f51ece853d30525fcd9a3e477c2194fedb549f"
    );

    let auxpow_header = deserialize_header(&auxpow_extended_header)
        .expect("AuxPoW-extended qbitd header should parse");
    assert_eq!(
        auxpow_header.block_hash().to_string(),
        "fd7f94d1992a159f2ff0311d92e23fa7f880ca285d56da6765340f04d3c88aca"
    );
}

#[test]
fn qbit_header_parser_rejects_whole_auxpow_block_bytes() {
    let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-qbitd-auxpow-block.hex");
    let extended_header =
        fixture_hex("tests/fixtures/qbit/headers/regtest-qbitd-auxpow-extended-header.hex");

    assert_eq!(
        deserialize_header(&raw),
        Err(Error::TrailingBytes {
            consumed: extended_header.len(),
            total: raw.len()
        })
    );
}

#[test]
fn qbit_non_auxpow_block_is_header_then_tx_vector() {
    let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-synthetic-non-auxpow-block.hex");
    let block = deserialize_block(&raw).expect("non-AuxPoW block should parse");

    assert!(!block.has_auxpow());
    assert_eq!(
        block.header.block_hash().to_string(),
        "4ddd9f0855d58a375be5a763e5f51ece853d30525fcd9a3e477c2194fedb549f"
    );
    assert_eq!(block.txdata.len(), 1);
    assert_eq!(block.serialized_size(), raw.len());
    assert_eq!(block.weight(), raw.len());
    assert_eq!(transaction_weight(&block.txdata[0]), 66);
}

#[test]
fn qbit_legacy_version_with_auxpow_bit_is_not_auxpow() {
    let mut raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-synthetic-non-auxpow-block.hex");
    raw[0..4].copy_from_slice(&0x0000_0100i32.to_le_bytes());

    let block = deserialize_block(&raw).expect("legacy version block should parse without AuxPoW");
    assert_eq!(block.header.version, 0x0000_0100);
    assert!(!block.has_auxpow());
    assert_eq!(block.txdata.len(), 1);
}

#[test]
fn qbit_auxpow_payload_is_consumed_before_tx_vector() {
    let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-qbitd-auxpow-block.hex");
    let pure_header = fixture_hex("tests/fixtures/qbit/headers/regtest-qbitd-auxpow-header.hex");
    let extended_header =
        fixture_hex("tests/fixtures/qbit/headers/regtest-qbitd-auxpow-extended-header.hex");
    let block = deserialize_block(&raw).expect("qbitd AuxPoW block should parse");

    assert_eq!(
        block.header.version & AUXPOW_VERSION_FLAG,
        AUXPOW_VERSION_FLAG
    );
    assert!(block.has_auxpow());
    assert_eq!(
        block.header.block_hash().to_string(),
        "fd7f94d1992a159f2ff0311d92e23fa7f880ca285d56da6765340f04d3c88aca"
    );
    assert_eq!(block.txdata.len(), 1);
    assert_eq!(pure_header, raw[..PURE_BLOCK_HEADER_LEN]);
    assert_eq!(extended_header, raw[..extended_header.len()]);

    let auxpow = block.auxpow.as_ref().expect("AuxPoW payload should exist");
    assert_eq!(auxpow.coinbase_tx.input.len(), 1);
    assert!(auxpow.coinbase_tx.input[0].witness.is_empty());
    assert!(auxpow.coinbase_merkle_branch.is_empty());
    assert_eq!(auxpow.coinbase_branch_index, 0);
    assert!(auxpow.chain_merkle_branch.is_empty());
    assert_eq!(auxpow.chain_index, 0);

    assert_eq!(serialize(&block.txdata[0]).len(), 180);
    assert_eq!(block.serialized_size(), raw.len());
    assert_eq!(block.weight(), raw.len());
}

#[test]
fn qbit_block_rejects_short_header() {
    assert_eq!(
        deserialize_block(&vec![0u8; PURE_BLOCK_HEADER_LEN - 1]),
        Err(Error::ShortBlockHeader {
            actual: PURE_BLOCK_HEADER_LEN - 1
        })
    );
}
