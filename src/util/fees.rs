use crate::chain::{Network, Transaction, TxOut};
use std::collections::HashMap;

const VSIZE_BIN_WIDTH: u32 = 50_000; // in vbytes, or qbit serialized bytes under WSF=1

pub struct TxFeeInfo {
    pub fee: u64,   // in satoshis
    pub vsize: u32, // Bitcoin-style vbytes, or qbit serialized bytes under WSF=1
    pub fee_per_vbyte: f32,
}

impl TxFeeInfo {
    pub fn new(tx: &Transaction, prevouts: &HashMap<u32, &TxOut>, network: Network) -> Self {
        let fee = get_tx_fee(tx, prevouts, network);
        let vsize = tx_vsize(tx, network);

        TxFeeInfo {
            fee,
            vsize,
            fee_per_vbyte: fee as f32 / vsize as f32,
        }
    }
}

pub fn tx_size(tx: &Transaction, _network: Network) -> u32 {
    #[cfg(not(feature = "liquid"))]
    if _network.is_qbit() {
        return crate::qbit_codec::transaction_size(tx) as u32;
    }

    tx.size() as u32
}

pub fn tx_weight(tx: &Transaction, _network: Network) -> u32 {
    #[cfg(not(feature = "liquid"))]
    if _network.is_qbit() {
        return crate::qbit_codec::transaction_weight(tx) as u32;
    }

    tx.weight() as u32
}

pub fn tx_vsize(tx: &Transaction, _network: Network) -> u32 {
    #[cfg(not(feature = "liquid"))]
    if _network.is_qbit() {
        return tx_weight(tx, _network);
    }

    (tx.weight() / 4) as u32
}

#[cfg(not(feature = "liquid"))]
pub fn get_tx_fee(tx: &Transaction, prevouts: &HashMap<u32, &TxOut>, _network: Network) -> u64 {
    if tx.is_coin_base() {
        return 0;
    }

    let total_in: u64 = prevouts.values().map(|prevout| prevout.value).sum();
    let total_out: u64 = tx.output.iter().map(|vout| vout.value).sum();
    total_in - total_out
}

#[cfg(feature = "liquid")]
pub fn get_tx_fee(tx: &Transaction, _prevouts: &HashMap<u32, &TxOut>, network: Network) -> u64 {
    tx.fee_in(*network.native_asset())
}

pub fn make_fee_histogram(mut entries: Vec<&TxFeeInfo>) -> Vec<(f32, u32)> {
    entries.sort_unstable_by(|e1, e2| e1.fee_per_vbyte.partial_cmp(&e2.fee_per_vbyte).unwrap());

    let mut histogram = vec![];
    let mut bin_size = 0;
    let mut last_fee_rate = 0.0;
    for e in entries.iter().rev() {
        if bin_size > VSIZE_BIN_WIDTH && last_fee_rate != e.fee_per_vbyte {
            // vsize of transactions paying >= last_fee_rate
            histogram.push((last_fee_rate, bin_size));
            bin_size = 0;
        }
        last_fee_rate = e.fee_per_vbyte;
        bin_size += e.vsize;
    }
    if bin_size > 0 {
        histogram.push((last_fee_rate, bin_size));
    }
    histogram
}

#[cfg(all(test, not(feature = "liquid")))]
mod tests {
    use super::*;
    use bitcoin::consensus::encode::deserialize;
    use bitcoin::hashes::hex::FromHex;
    use bitcoin::{OutPoint, Script, TxIn, Txid, Witness};

    fn qbit_witness_fixture() -> Transaction {
        let hex = include_str!("../../tests/fixtures/qbit/transactions/regtest-witness-tx.hex");
        let raw = hex::decode(hex.split_whitespace().collect::<String>()).unwrap();
        deserialize(&raw).expect("qbit witness fixture should parse")
    }

    fn txid() -> Txid {
        Txid::from_hex("0101010101010101010101010101010101010101010101010101010101010101").unwrap()
    }

    fn p2mr_witness_tx(arg_bytes: usize, arg_count: usize) -> Transaction {
        let mut witness = Vec::new();
        for _ in 0..arg_count {
            witness.push(vec![0x51; arg_bytes]);
        }
        let mut leaf_script = vec![0x20];
        leaf_script.extend_from_slice(&[0x11; 32]);
        leaf_script.push(0xb3);
        witness.push(leaf_script);
        witness.push(vec![0xc1]);

        Transaction {
            version: 2,
            lock_time: 0,
            input: vec![TxIn {
                previous_output: OutPoint {
                    txid: txid(),
                    vout: 0,
                },
                script_sig: Script::new(),
                sequence: 0xffff_ffff,
                witness: Witness::from_vec(witness),
            }],
            output: vec![TxOut {
                value: 0,
                script_pubkey: Script::new(),
            }],
        }
    }

    fn prevouts<'a>(prevout: &'a TxOut) -> HashMap<u32, &'a TxOut> {
        let mut prevouts = HashMap::new();
        prevouts.insert(0, prevout);
        prevouts
    }

    fn p2mr_prevout(value: u64) -> TxOut {
        TxOut {
            value,
            script_pubkey: Script::from(
                hex::decode("52200000000000000000000000000000000000000000000000000000000000000000")
                    .unwrap(),
            ),
        }
    }

    #[test]
    fn qbit_fixture_tx_reports_size_weight_and_vsize_as_serialized_bytes() {
        let tx = qbit_witness_fixture();

        assert_eq!(tx_size(&tx, Network::QbitRegtest), 66);
        assert_eq!(tx_weight(&tx, Network::QbitRegtest), 66);
        assert_eq!(tx_vsize(&tx, Network::QbitRegtest), 66);
        assert_ne!(tx_weight(&tx, Network::QbitRegtest), tx.weight() as u32);
        assert_ne!(
            tx_vsize(&tx, Network::QbitRegtest),
            (tx.weight() / 4) as u32
        );
    }

    #[test]
    fn qbit_fee_info_uses_byte_vsize_for_large_witness_transactions() {
        let tx = p2mr_witness_tx(16_000, 4);
        let prevout = p2mr_prevout(120_000);
        let prevouts = prevouts(&prevout);

        let qbit = TxFeeInfo::new(&tx, &prevouts, Network::QbitRegtest);
        let bitcoin_discounted_vsize = (tx.weight() / 4) as u32;

        assert_eq!(qbit.fee, 120_000);
        assert_eq!(qbit.vsize, tx_size(&tx, Network::QbitRegtest));
        assert!(qbit.vsize > bitcoin_discounted_vsize);
        assert!((qbit.fee_per_vbyte - qbit.fee as f32 / qbit.vsize as f32).abs() < f32::EPSILON);
    }

    #[test]
    fn fee_histogram_uses_qbit_byte_vsize_bins() {
        let tx = p2mr_witness_tx(16_000, 4);
        let prevout = p2mr_prevout(120_000);
        let prevouts = prevouts(&prevout);
        let high_fee_qbit = TxFeeInfo::new(&tx, &prevouts, Network::QbitRegtest);
        let low_fee = TxFeeInfo {
            fee: 1,
            vsize: 1,
            fee_per_vbyte: 1.0,
        };

        assert!(high_fee_qbit.vsize > VSIZE_BIN_WIDTH);

        let histogram = make_fee_histogram(vec![&low_fee, &high_fee_qbit]);

        assert_eq!(
            histogram[0],
            (high_fee_qbit.fee_per_vbyte, high_fee_qbit.vsize)
        );
    }
}
