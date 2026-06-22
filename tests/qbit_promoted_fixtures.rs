use bitcoin::consensus::encode::serialize;
use electrs::qbit_codec::{
    deserialize_block, deserialize_header, deserialize_pure_header, deserialize_transaction,
    transaction_size, transaction_weight, PURE_BLOCK_HEADER_LEN,
};
use serde_json::Value;

fn fixture_hex(path: &str) -> Vec<u8> {
    let hex = std::fs::read_to_string(path).expect("fixture hex should be readable");
    hex::decode(hex.split_whitespace().collect::<String>()).expect("fixture hex should decode")
}

fn fixture_json(path: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture JSON should be readable"))
        .expect("fixture JSON should parse")
}

fn manifest() -> Value {
    fixture_json("tests/fixtures/qbit/manifest.json")
}

fn manifest_fixture<'a>(manifest: &'a Value, id: &str) -> &'a Value {
    manifest["fixtures"]
        .as_array()
        .expect("manifest fixtures should be an array")
        .iter()
        .find(|fixture| fixture["id"] == id)
        .unwrap_or_else(|| panic!("missing promoted fixture {}", id))
}

fn as_u64(value: &Value) -> u64 {
    value.as_u64().expect("manifest value should be u64")
}

fn btc_to_sats(value: &Value) -> u64 {
    let value = value.as_f64().expect("fee value should be numeric");
    (value * 100_000_000.0).round() as u64
}

fn as_vec_u64(value: &Value) -> Vec<u64> {
    value
        .as_array()
        .expect("manifest value should be an array")
        .iter()
        .map(as_u64)
        .collect()
}

fn assert_boundary_transaction(
    manifest: &Value,
    fixture_id: &str,
    expected_stack_lengths: &[u64],
    accepted: bool,
) -> (String, u64) {
    let fixture = manifest_fixture(manifest, fixture_id);
    let tx_hex = fixture_hex(fixture["path"].as_str().unwrap());
    let tx = deserialize_transaction(&tx_hex).expect("P2MR boundary tx should parse");
    let facts = &fixture["expected_parser_facts"];
    let txid = fixture["txid"].as_str().expect("boundary txid").to_string();
    let tx_size = tx_hex.len() as u64;

    assert_eq!(serialize(&tx), tx_hex);
    assert_eq!(tx.txid().to_string(), txid);
    assert_eq!(transaction_size(&tx) as u64, tx_size);
    assert_eq!(transaction_weight(&tx) as u64, tx_size);
    assert_eq!(as_u64(&facts["serialized_size"]), tx_size);
    assert_eq!(as_u64(&facts["qbit_weight"]), tx_size);
    assert_eq!(as_u64(&facts["qbit_vsize"]), tx_size);
    assert_eq!(as_u64(&facts["fee_sats"]), tx_size);
    assert!(
        tx_size > as_u64(&facts["bitcoin_wsf4_vsize"]),
        "qbit size should be larger than Bitcoin WSF=4 discounted vsize"
    );
    assert_eq!(facts["p2mr_witness"].as_bool(), Some(true));
    assert_eq!(as_u64(&facts["witness_scale_factor"]), 1);
    assert_eq!(
        as_vec_u64(&facts["initial_stack_item_lengths"]),
        expected_stack_lengths.to_vec()
    );
    assert_eq!(
        as_u64(&facts["initial_stack_item_count"]),
        expected_stack_lengths.len() as u64
    );
    assert_eq!(
        as_u64(&facts["initial_stack_total_bytes"]),
        expected_stack_lengths.iter().sum::<u64>()
    );
    assert_eq!(
        as_u64(&facts["max_initial_stack_item_bytes"]),
        *expected_stack_lengths
            .iter()
            .max()
            .expect("boundary stack should be non-empty")
    );
    assert_eq!(as_u64(&facts["max_standard_stack_item_bytes"]), 16 * 1024);
    assert_eq!(
        as_u64(&facts["max_standard_total_initial_stack_bytes"]),
        128 * 1024
    );

    assert_eq!(tx.input.len(), 1);
    let witness = tx.input[0].witness.iter().collect::<Vec<_>>();
    let initial_count = expected_stack_lengths.len();
    assert_eq!(witness.len(), initial_count + 2);
    for item in witness.iter().take(initial_count) {
        assert!(
            item.iter().all(|byte| *byte == 0x42),
            "boundary stack items should use deterministic 0x42 filler"
        );
    }
    let leaf_script = witness[initial_count];
    let control_block = witness[initial_count + 1];
    assert_eq!(hex::encode(leaf_script), facts["leaf_script_hex"]);
    assert_eq!(hex::encode(control_block), facts["control_block_hex"]);
    assert_eq!(control_block[0] & 1, 1);
    assert_eq!(control_block[0] & 0xfe, 0xc0);

    if !accepted {
        assert_eq!(facts["expected_reject_reason"], "bad-witness-nonstandard");
    }

    (txid, tx_size)
}

#[test]
fn promoted_qbitd_p2mr_fixtures_match_manifest_and_rpc_truth() {
    let manifest = manifest();
    let tx_fixture = manifest_fixture(&manifest, "regtest-qbitd-p2mr-spend-tx");
    let block_fixture = manifest_fixture(&manifest, "regtest-qbitd-p2mr-spend-block");
    let tx_rpc_fixture = manifest_fixture(&manifest, "regtest-qbitd-getrawtransaction-p2mr-spend");
    let block_rpc_fixture = manifest_fixture(&manifest, "regtest-qbitd-getblock-p2mr-spend");
    let header_rpc_fixture = manifest_fixture(&manifest, "regtest-qbitd-getblockheader-p2mr-spend");
    let mempool_rpc_fixture =
        manifest_fixture(&manifest, "regtest-qbitd-getmempoolentry-p2mr-spend");
    let rawmempool_rpc_fixture =
        manifest_fixture(&manifest, "regtest-qbitd-getrawmempool-with-p2mr-spend");

    let tx_hex = fixture_hex(tx_fixture["path"].as_str().unwrap());
    let tx = deserialize_transaction(&tx_hex).expect("promoted P2MR spend should parse");
    let tx_rpc = fixture_json(tx_rpc_fixture["path"].as_str().unwrap());
    let mempool_rpc = fixture_json(mempool_rpc_fixture["path"].as_str().unwrap());

    let txid = tx_fixture["txid"].as_str().expect("promoted txid");
    assert_eq!(tx.txid().to_string(), txid);
    assert_eq!(tx_rpc["txid"], txid);
    assert_eq!(mempool_rpc_fixture["txid"], txid);
    assert_eq!(mempool_rpc_fixture["expected_parser_facts"]["txid"], txid);
    assert_eq!(rawmempool_rpc_fixture["txid"], txid);
    assert_eq!(
        rawmempool_rpc_fixture["expected_parser_facts"]["contains_txid"],
        txid
    );

    let tx_size = tx_hex.len() as u64;
    assert_eq!(serialize(&tx), tx_hex);
    assert_eq!(transaction_size(&tx) as u64, tx_size);
    assert_eq!(transaction_weight(&tx) as u64, tx_size);
    assert_eq!(
        as_u64(&tx_fixture["expected_parser_facts"]["serialized_size"]),
        tx_size
    );
    assert_eq!(
        as_u64(&tx_fixture["expected_parser_facts"]["qbit_weight"]),
        tx_size
    );
    assert_eq!(as_u64(&tx_rpc["size"]), tx_size);
    assert_eq!(as_u64(&tx_rpc["vsize"]), tx_size);
    assert_eq!(as_u64(&tx_rpc["weight"]), tx_size);
    for field in ["vsize", "weight", "ancestorsize", "descendantsize"] {
        assert_eq!(as_u64(&mempool_rpc[field]), tx_size, "mempool {}", field);
    }
    assert_eq!(btc_to_sats(&mempool_rpc["fees"]["base"]), tx_size);
    assert_eq!(tx.input.len() as u64, 1);
    assert!(tx.input[0].witness.iter().next().is_some());

    let rawmempool_rpc = fixture_json(rawmempool_rpc_fixture["path"].as_str().unwrap());
    assert!(
        rawmempool_rpc
            .as_array()
            .expect("raw mempool fixture should be an array")
            .iter()
            .any(|entry| entry.as_str() == Some(txid)),
        "raw mempool fixture should contain promoted P2MR spend"
    );

    let block_hex = fixture_hex(block_fixture["path"].as_str().unwrap());
    let block = deserialize_block(&block_hex).expect("promoted P2MR spend block should parse");
    let block_rpc = fixture_json(block_rpc_fixture["path"].as_str().unwrap());
    let header_rpc = fixture_json(header_rpc_fixture["path"].as_str().unwrap());

    let blockhash = block_fixture["blockhash"]
        .as_str()
        .expect("promoted blockhash");
    assert_eq!(block.header.block_hash().to_string(), blockhash);
    assert_eq!(block_rpc["hash"], blockhash);
    assert_eq!(header_rpc["hash"], blockhash);
    assert_eq!(block_rpc_fixture["blockhash"], blockhash);
    assert_eq!(header_rpc_fixture["blockhash"], blockhash);

    let block_size = block_hex.len() as u64;
    assert_eq!(block.serialized_size() as u64, block_size);
    assert_eq!(block.weight() as u64, block_size);
    assert_eq!(
        as_u64(&block_fixture["expected_parser_facts"]["serialized_size"]),
        block_size
    );
    assert_eq!(
        as_u64(&block_fixture["expected_parser_facts"]["qbit_weight"]),
        block_size
    );
    assert_eq!(as_u64(&block_rpc["size"]), block_size);
    assert_eq!(as_u64(&block_rpc["weight"]), block_size);
    assert_eq!(as_u64(&block_rpc["height"]), 1002);
    assert_eq!(as_u64(&header_rpc["height"]), 1002);
    assert_eq!(
        as_u64(&block_fixture["expected_parser_facts"]["pure_header_bytes"]),
        PURE_BLOCK_HEADER_LEN as u64
    );
    assert!(!block.has_auxpow());
    assert_eq!(block.txdata.len() as u64, 2);
    assert_eq!(
        as_u64(&block_fixture["expected_parser_facts"]["tx_count"]),
        block.txdata.len() as u64
    );
    assert!(
        block
            .txdata
            .iter()
            .any(|block_tx| block_tx.txid().to_string() == txid),
        "promoted block should contain promoted P2MR spend"
    );
    assert!(
        block_rpc["tx"]
            .as_array()
            .expect("getblock tx field should be an array")
            .iter()
            .any(|entry| entry.as_str() == Some(txid)),
        "getblock RPC fixture should contain promoted P2MR spend"
    );
}

#[test]
fn promoted_qbitd_p2mr_boundary_fixtures_match_manifest_and_rpc_truth() {
    let manifest = manifest();
    let stack_16k = [16 * 1024];
    let stack_128k = [16 * 1024; 8];
    let stack_item_oversize = [16 * 1024 + 1];
    let stack_total_oversize = [
        16 * 1024,
        16 * 1024,
        16 * 1024,
        16 * 1024,
        16 * 1024,
        16 * 1024,
        16 * 1024,
        16 * 1024,
        1,
    ];

    let (stack_16k_txid, stack_16k_size) = assert_boundary_transaction(
        &manifest,
        "regtest-qbitd-p2mr-stack-16k-spend-tx",
        &stack_16k,
        true,
    );
    let (stack_128k_txid, stack_128k_size) = assert_boundary_transaction(
        &manifest,
        "regtest-qbitd-p2mr-stack-128k-spend-tx",
        &stack_128k,
        true,
    );
    let (stack_item_oversize_txid, _) = assert_boundary_transaction(
        &manifest,
        "regtest-qbitd-p2mr-stack-item-oversize-reject-tx",
        &stack_item_oversize,
        false,
    );
    let (stack_total_oversize_txid, _) = assert_boundary_transaction(
        &manifest,
        "regtest-qbitd-p2mr-stack-total-oversize-reject-tx",
        &stack_total_oversize,
        false,
    );

    for (accept_id, txid, tx_size) in [
        (
            "regtest-qbitd-testmempoolaccept-p2mr-stack-16k",
            stack_16k_txid.as_str(),
            stack_16k_size,
        ),
        (
            "regtest-qbitd-testmempoolaccept-p2mr-stack-128k",
            stack_128k_txid.as_str(),
            stack_128k_size,
        ),
    ] {
        let accept_fixture = manifest_fixture(&manifest, accept_id);
        let accept_rpc = fixture_json(accept_fixture["path"].as_str().unwrap());
        assert_eq!(accept_fixture["txid"], txid);
        assert_eq!(accept_fixture["expected_parser_facts"]["txid"], txid);
        assert_eq!(accept_rpc["allowed"].as_bool(), Some(true));
        assert_eq!(accept_rpc["txid"], txid);
        assert_eq!(as_u64(&accept_rpc["vsize"]), tx_size);
        assert_eq!(btc_to_sats(&accept_rpc["fees"]["base"]), tx_size);
    }

    for (mempool_id, txid, tx_size) in [
        (
            "regtest-qbitd-getmempoolentry-p2mr-stack-16k",
            stack_16k_txid.as_str(),
            stack_16k_size,
        ),
        (
            "regtest-qbitd-getmempoolentry-p2mr-stack-128k",
            stack_128k_txid.as_str(),
            stack_128k_size,
        ),
    ] {
        let mempool_fixture = manifest_fixture(&manifest, mempool_id);
        let mempool_rpc = fixture_json(mempool_fixture["path"].as_str().unwrap());
        assert_eq!(mempool_fixture["txid"], txid);
        assert_eq!(mempool_fixture["expected_parser_facts"]["txid"], txid);
        for field in ["vsize", "weight", "ancestorsize", "descendantsize"] {
            assert_eq!(as_u64(&mempool_rpc[field]), tx_size, "mempool {}", field);
        }
        assert_eq!(btc_to_sats(&mempool_rpc["fees"]["base"]), tx_size);
    }

    for (accept_id, txid) in [
        (
            "regtest-qbitd-testmempoolaccept-p2mr-stack-item-oversize",
            stack_item_oversize_txid.as_str(),
        ),
        (
            "regtest-qbitd-testmempoolaccept-p2mr-stack-total-oversize",
            stack_total_oversize_txid.as_str(),
        ),
    ] {
        let accept_fixture = manifest_fixture(&manifest, accept_id);
        let accept_rpc = fixture_json(accept_fixture["path"].as_str().unwrap());
        assert_eq!(accept_fixture["txid"], txid);
        assert_eq!(accept_fixture["expected_parser_facts"]["txid"], txid);
        assert_eq!(accept_rpc["allowed"].as_bool(), Some(false));
        assert_eq!(accept_rpc["txid"], txid);
        assert_eq!(accept_rpc["reject-reason"], "bad-witness-nonstandard");
    }

    let rawmempool_fixture = manifest_fixture(
        &manifest,
        "regtest-qbitd-getrawmempool-with-p2mr-boundary-spends",
    );
    let rawmempool_rpc = fixture_json(rawmempool_fixture["path"].as_str().unwrap());
    for txid in [&stack_16k_txid, &stack_128k_txid] {
        assert!(
            rawmempool_rpc
                .as_array()
                .expect("raw mempool fixture should be an array")
                .iter()
                .any(|entry| entry.as_str() == Some(txid)),
            "raw mempool fixture should contain boundary tx {}",
            txid
        );
    }

    let block_fixture = manifest_fixture(&manifest, "regtest-qbitd-p2mr-boundary-spends-block");
    let block_rpc_fixture =
        manifest_fixture(&manifest, "regtest-qbitd-getblock-p2mr-boundary-spends");
    let header_rpc_fixture = manifest_fixture(
        &manifest,
        "regtest-qbitd-getblockheader-p2mr-boundary-spends",
    );
    let block_hex = fixture_hex(block_fixture["path"].as_str().unwrap());
    let block = deserialize_block(&block_hex).expect("P2MR boundary block should parse");
    let block_rpc = fixture_json(block_rpc_fixture["path"].as_str().unwrap());
    let header_rpc = fixture_json(header_rpc_fixture["path"].as_str().unwrap());
    let blockhash = block_fixture["blockhash"].as_str().unwrap();
    let block_size = block_hex.len() as u64;

    assert!(!block.has_auxpow());
    assert_eq!(block.header.block_hash().to_string(), blockhash);
    assert_eq!(block.serialized_size() as u64, block_size);
    assert_eq!(block.weight() as u64, block_size);
    assert_eq!(
        as_u64(&block_fixture["expected_parser_facts"]["serialized_size"]),
        block_size
    );
    assert_eq!(
        as_u64(&block_fixture["expected_parser_facts"]["qbit_weight"]),
        block_size
    );
    assert_eq!(as_u64(&block_rpc["size"]), block_size);
    assert_eq!(as_u64(&block_rpc["weight"]), block_size);
    assert_eq!(as_u64(&block_rpc["nTx"]), 3);
    assert_eq!(
        as_u64(&block_fixture["expected_parser_facts"]["tx_count"]),
        3
    );
    assert_eq!(block_rpc["hash"], blockhash);
    assert_eq!(header_rpc["hash"], blockhash);

    for txid in [&stack_16k_txid, &stack_128k_txid] {
        assert!(
            block
                .txdata
                .iter()
                .any(|block_tx| block_tx.txid().to_string() == *txid),
            "boundary block should contain tx {}",
            txid
        );
        assert!(
            block_rpc["tx"]
                .as_array()
                .expect("getblock tx field should be an array")
                .iter()
                .any(|entry| entry.as_str() == Some(txid)),
            "getblock RPC fixture should contain tx {}",
            txid
        );
    }
}

#[test]
fn promoted_qbitd_sigop_adjusted_vsize_fixture_matches_rpc_truth() {
    let manifest = manifest();
    let tx_fixture = manifest_fixture(
        &manifest,
        "regtest-qbitd-p2wsh-sigop-adjusted-vsize-spend-tx",
    );
    let accept_fixture = manifest_fixture(
        &manifest,
        "regtest-qbitd-testmempoolaccept-p2wsh-sigop-adjusted-vsize",
    );
    let mempool_fixture = manifest_fixture(
        &manifest,
        "regtest-qbitd-getmempoolentry-p2wsh-sigop-adjusted-vsize",
    );
    let raw_tx_fixture = manifest_fixture(
        &manifest,
        "regtest-qbitd-getrawtransaction-p2wsh-sigop-adjusted-vsize",
    );
    let rawmempool_fixture = manifest_fixture(
        &manifest,
        "regtest-qbitd-getrawmempool-with-sigop-adjusted-vsize",
    );

    let tx_hex = fixture_hex(tx_fixture["path"].as_str().unwrap());
    let tx = deserialize_transaction(&tx_hex).expect("sigop-adjusted vsize tx should parse");
    let accept_rpc = fixture_json(accept_fixture["path"].as_str().unwrap());
    let mempool_rpc = fixture_json(mempool_fixture["path"].as_str().unwrap());
    let raw_tx_rpc = fixture_json(raw_tx_fixture["path"].as_str().unwrap());
    let rawmempool_rpc = fixture_json(rawmempool_fixture["path"].as_str().unwrap());
    let facts = &tx_fixture["expected_parser_facts"];

    let txid = tx_fixture["txid"].as_str().expect("sigop fixture txid");
    let tx_size = tx_hex.len() as u64;
    let sigop_adjusted_vsize = as_u64(&facts["sigop_adjusted_vsize"]);

    assert_eq!(serialize(&tx), tx_hex);
    assert_eq!(tx.txid().to_string(), txid);
    assert_eq!(transaction_size(&tx) as u64, tx_size);
    assert_eq!(transaction_weight(&tx) as u64, tx_size);
    assert_eq!(as_u64(&facts["serialized_size"]), tx_size);
    assert_eq!(as_u64(&facts["qbit_weight"]), tx_size);
    assert_eq!(as_u64(&facts["qbit_tx_vsize"]), tx_size);
    assert_eq!(as_u64(&facts["witness_scale_factor"]), 1);
    assert_eq!(as_u64(&facts["sigop_cost"]), 222);
    assert_eq!(as_u64(&facts["bytes_per_sigop"]), 20);
    assert_eq!(sigop_adjusted_vsize, 4440);
    assert!(sigop_adjusted_vsize > tx_size);
    assert_eq!(facts["p2mr_witness"].as_bool(), Some(false));
    assert_eq!(
        facts["mempool_vsize_exceeds_serialized_size"].as_bool(),
        Some(true)
    );
    assert_eq!(tx.input.len(), 1);
    assert_eq!(tx.input[0].witness.iter().count(), 1);
    assert_eq!(
        hex::encode(tx.input[0].witness.iter().next().unwrap()),
        facts["witness_script_hex"]
    );

    assert_eq!(accept_fixture["txid"], txid);
    assert_eq!(accept_rpc["allowed"].as_bool(), Some(true));
    assert_eq!(accept_rpc["txid"], txid);
    assert_eq!(as_u64(&accept_rpc["vsize"]), sigop_adjusted_vsize);
    assert_eq!(
        btc_to_sats(&accept_rpc["fees"]["base"]),
        sigop_adjusted_vsize
    );

    assert_eq!(mempool_fixture["txid"], txid);
    assert_eq!(mempool_rpc["wtxid"], accept_rpc["wtxid"]);
    assert_eq!(as_u64(&mempool_rpc["vsize"]), sigop_adjusted_vsize);
    assert_eq!(as_u64(&mempool_rpc["weight"]), tx_size);
    assert_eq!(as_u64(&mempool_rpc["ancestorsize"]), sigop_adjusted_vsize);
    assert_eq!(as_u64(&mempool_rpc["descendantsize"]), sigop_adjusted_vsize);
    assert_eq!(
        btc_to_sats(&mempool_rpc["fees"]["base"]),
        sigop_adjusted_vsize
    );

    assert_eq!(raw_tx_rpc["txid"], txid);
    assert_eq!(as_u64(&raw_tx_rpc["size"]), tx_size);
    assert_eq!(as_u64(&raw_tx_rpc["vsize"]), tx_size);
    assert_eq!(as_u64(&raw_tx_rpc["weight"]), tx_size);
    assert!(
        rawmempool_rpc
            .as_array()
            .expect("raw mempool fixture should be an array")
            .iter()
            .any(|entry| entry.as_str() == Some(txid)),
        "raw mempool fixture should contain sigop-adjusted vsize tx"
    );
}

#[test]
fn promoted_qbitd_auxpow_fixtures_match_manifest_and_rpc_truth() {
    let manifest = manifest();
    let block_fixture = manifest_fixture(&manifest, "regtest-qbitd-auxpow-block");
    let pure_header_fixture = manifest_fixture(&manifest, "regtest-qbitd-auxpow-pure-header");
    let extended_header_fixture =
        manifest_fixture(&manifest, "regtest-qbitd-auxpow-extended-header");
    let payload_fixture = manifest_fixture(&manifest, "regtest-qbitd-auxpow-payload");
    let block_rpc_fixture = manifest_fixture(&manifest, "regtest-qbitd-getblock-auxpow");
    let header_rpc_fixture = manifest_fixture(&manifest, "regtest-qbitd-getblockheader-auxpow");
    let template_rpc_fixture = manifest_fixture(&manifest, "regtest-qbitd-createauxblock-template");

    let raw_block = fixture_hex(block_fixture["path"].as_str().unwrap());
    let pure_header = fixture_hex(pure_header_fixture["path"].as_str().unwrap());
    let extended_header = fixture_hex(extended_header_fixture["path"].as_str().unwrap());
    let auxpow_payload = fixture_hex(payload_fixture["path"].as_str().unwrap());
    let block = deserialize_block(&raw_block).expect("promoted AuxPoW block should parse");

    let facts = &block_fixture["expected_parser_facts"];
    let blockhash = block_fixture["blockhash"]
        .as_str()
        .expect("promoted AuxPoW blockhash");
    assert!(block.has_auxpow());
    assert_eq!(block.header.block_hash().to_string(), blockhash);
    assert_eq!(
        block.serialized_size() as u64,
        as_u64(&facts["serialized_size"])
    );
    assert_eq!(block.weight() as u64, as_u64(&facts["qbit_weight"]));
    assert_eq!(raw_block.len() as u64, as_u64(&facts["serialized_size"]));
    assert_eq!(block.txdata.len() as u64, as_u64(&facts["tx_count"]));
    assert_eq!(
        as_u64(&facts["pure_header_bytes"]),
        PURE_BLOCK_HEADER_LEN as u64
    );
    assert_eq!(as_u64(&facts["auxpow_chain_id"]), 31430);

    assert_eq!(pure_header.len(), PURE_BLOCK_HEADER_LEN);
    assert_eq!(pure_header, raw_block[..PURE_BLOCK_HEADER_LEN]);
    assert_eq!(hex::encode(&pure_header), facts["header_hex"]);
    assert_eq!(
        deserialize_pure_header(&pure_header)
            .expect("pure AuxPoW header should parse")
            .block_hash()
            .to_string(),
        blockhash
    );

    let extended_header_len = as_u64(&facts["auxpow_extended_header_bytes"]) as usize;
    assert_eq!(extended_header.len(), extended_header_len);
    assert_eq!(extended_header, raw_block[..extended_header_len]);
    assert_eq!(
        extended_header,
        [&pure_header[..], &auxpow_payload[..]].concat()
    );
    assert_eq!(hex::encode(&extended_header), facts["extended_header_hex"]);
    assert_eq!(
        deserialize_header(&extended_header)
            .expect("AuxPoW-extended header should parse")
            .block_hash()
            .to_string(),
        blockhash
    );

    let auxpow = block.auxpow.as_ref().expect("AuxPoW payload should exist");
    assert_eq!(auxpow.coinbase_tx.input.len(), 1);
    assert!(auxpow.coinbase_merkle_branch.is_empty());
    assert_eq!(auxpow.coinbase_branch_index, 0);
    assert!(auxpow.chain_merkle_branch.is_empty());
    assert_eq!(auxpow.chain_index, 0);
    assert_eq!(
        auxpow_payload.len() as u64,
        as_u64(&payload_fixture["expected_parser_facts"]["serialized_size"])
    );

    let block_rpc = fixture_json(block_rpc_fixture["path"].as_str().unwrap());
    let header_rpc = fixture_json(header_rpc_fixture["path"].as_str().unwrap());
    let template_rpc = fixture_json(template_rpc_fixture["path"].as_str().unwrap());
    let header_rpc_facts = &header_rpc_fixture["expected_parser_facts"];
    assert_eq!(block_rpc["hash"], blockhash);
    assert_eq!(header_rpc["hash"], blockhash);
    assert_eq!(template_rpc["hash"], blockhash);
    assert_eq!(as_u64(&block_rpc["height"]), 102);
    assert_eq!(as_u64(&header_rpc["height"]), 102);
    assert_eq!(as_u64(&template_rpc["height"]), 102);
    assert_eq!(as_u64(&block_rpc["size"]), raw_block.len() as u64);
    assert_eq!(as_u64(&block_rpc["weight"]), raw_block.len() as u64);
    assert_eq!(as_u64(&block_rpc["nTx"]), block.txdata.len() as u64);
    assert_eq!(as_u64(&template_rpc["chainid"]), 31430);
    assert_eq!(
        header_rpc_facts["verbose_json_has_no_serialized_header"].as_bool(),
        Some(true)
    );
    assert!(header_rpc_facts.get("header_bytes").is_none());
    assert_eq!(
        header_rpc_facts["pure_header_fixture"],
        "regtest-qbitd-auxpow-pure-header"
    );
    assert_eq!(
        header_rpc_facts["extended_header_fixture"],
        "regtest-qbitd-auxpow-extended-header"
    );
}

#[test]
fn manifest_catalog_includes_promoted_qbitd_bundle() {
    let manifest = manifest();
    let fixtures = manifest["fixtures"]
        .as_array()
        .expect("manifest fixtures should be an array");

    for issue in [4, 14, 20] {
        assert!(
            manifest["contract"]["issues"]
                .as_array()
                .expect("contract issues should be an array")
                .iter()
                .any(|value| value.as_i64() == Some(issue)),
            "manifest should anchor promoted qbitd bundle to issue {}",
            issue
        );
    }

    for fixture_type in ["transaction", "block", "metadata", "rpc"] {
        assert!(
            fixtures.iter().any(|fixture| {
                fixture["source"]["kind"] == "qbitd-generated"
                    && fixture["fixture_type"] == fixture_type
            }),
            "manifest should include a promoted qbitd-generated {} fixture",
            fixture_type
        );
    }
}
