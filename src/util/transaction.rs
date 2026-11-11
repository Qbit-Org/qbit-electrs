use crate::chain::{BlockHash, OutPoint, Transaction, TxIn, TxOut, Txid};
use crate::errors;
use crate::util::BlockId;

use std::collections::HashMap;

#[cfg(feature = "liquid")]
use bitcoin::hashes::hex::FromHex;

#[cfg(feature = "liquid")]
lazy_static! {
    static ref REGTEST_INITIAL_ISSUANCE_PREVOUT: Txid =
        Txid::from_hex("50cdc410c9d0d61eeacc531f52d2c70af741da33af127c364e52ac1ee7c030a5").unwrap();
    static ref TESTNET_INITIAL_ISSUANCE_PREVOUT: Txid =
        Txid::from_hex("0c52d2526a5c9f00e9fb74afd15dd3caaf17c823159a514f929ae25193a43a52").unwrap();
}

#[derive(Serialize, Deserialize)]
pub struct TransactionStatus {
    pub confirmed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub block_height: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub block_hash: Option<BlockHash>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub block_time: Option<u32>,
}

impl From<Option<BlockId>> for TransactionStatus {
    fn from(blockid: Option<BlockId>) -> TransactionStatus {
        match blockid {
            Some(b) => TransactionStatus {
                confirmed: true,
                block_height: Some(b.height),
                block_hash: Some(b.hash),
                block_time: Some(b.time),
            },
            None => TransactionStatus {
                confirmed: false,
                block_height: None,
                block_hash: None,
                block_time: None,
            },
        }
    }
}

#[derive(Serialize, Deserialize)]
pub struct TxInput {
    pub txid: Txid,
    pub vin: u32,
}

pub fn is_coinbase(txin: &TxIn) -> bool {
    #[cfg(not(feature = "liquid"))]
    return txin.previous_output.is_null();
    #[cfg(feature = "liquid")]
    return txin.is_coinbase();
}

pub fn has_prevout(txin: &TxIn) -> bool {
    #[cfg(not(feature = "liquid"))]
    return !txin.previous_output.is_null();
    #[cfg(feature = "liquid")]
    return !txin.is_coinbase()
        && !txin.is_pegin
        && txin.previous_output.txid != *REGTEST_INITIAL_ISSUANCE_PREVOUT
        && txin.previous_output.txid != *TESTNET_INITIAL_ISSUANCE_PREVOUT;
}

pub fn is_spendable(txout: &TxOut) -> bool {
    #[cfg(not(feature = "liquid"))]
    return !txout.script_pubkey.is_provably_unspendable();
    #[cfg(feature = "liquid")]
    return !txout.is_fee() && !txout.script_pubkey.is_provably_unspendable();
}

/// Extract the previous TxOuts of a Transaction's TxIns
///
/// # Errors
///
/// This function MUST NOT return an error variant when allow_missing is true.
/// If allow_missing is false, it will return an error when any Outpoint is
/// missing from the keys of the txos argument's HashMap.
pub fn extract_tx_prevouts<'a>(
    tx: &Transaction,
    txos: &'a HashMap<OutPoint, TxOut>,
) -> Result<HashMap<u32, &'a TxOut>, errors::Error> {
    tx.input
        .iter()
        .enumerate()
        .filter(|(_, txi)| has_prevout(txi))
        .map(|(index, txi)| {
            Ok((
                index as u32,
                match txos.get(&txi.previous_output) {
                    Some(txo) => txo,
                    None => {
                        return Err(format!("missing outpoint {:?}", txi.previous_output).into());
                    }
                },
            ))
        })
        .collect()
}

pub fn serialize_outpoint<S>(outpoint: &OutPoint, serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::ser::Serializer,
{
    use serde::ser::SerializeStruct;
    let mut s = serializer.serialize_struct("OutPoint", 2)?;
    s.serialize_field("txid", &outpoint.txid)?;
    s.serialize_field("vout", &outpoint.vout)?;
    s.end()
}

pub(super) mod sigops {
    use crate::chain::{
        hashes::hex::FromHex,
        opcodes::{
            all::{OP_CHECKMULTISIG, OP_CHECKMULTISIGVERIFY, OP_CHECKSIG, OP_CHECKSIGVERIFY},
            All,
        },
        script::{self, Instruction},
        Network, Transaction, TxOut, Witness,
    };
    use std::collections::HashMap;

    /// Get the sigop cost of a transaction as the network's node counts it
    /// (GetTransactionSigOpCost): its legacy and P2SH sigops count the network's witness
    /// scale factor times each, and its witness sigops once. prevout_map must have all the
    /// prevouts.
    pub fn transaction_sigop_count(
        tx: &Transaction,
        prevout_map: &HashMap<u32, &TxOut>,
        network: Network,
    ) -> Result<usize, script::Error> {
        let input_count = tx.input.len();
        let mut prevouts = Vec::with_capacity(input_count);

        #[cfg(not(feature = "liquid"))]
        let is_coinbase_or_pegin = tx.is_coin_base();
        #[cfg(feature = "liquid")]
        let is_coinbase_or_pegin = tx.is_coinbase() || tx.input.iter().any(|input| input.is_pegin);

        if !is_coinbase_or_pegin {
            for idx in 0..input_count {
                prevouts.push(
                    *prevout_map
                        .get(&(idx as u32))
                        .ok_or(script::Error::EarlyEndOfScript)?,
                );
            }
        }

        // coinbase tx won't use prevouts so it can be empty.
        get_sigop_cost(tx, &prevouts, true, true, witness_scale_factor(network))
    }

    /// Bitcoin's and Elements' WITNESS_SCALE_FACTOR is 4. qbit's is 1, so qbitd counts a
    /// legacy or P2SH sigop once.
    fn witness_scale_factor(network: Network) -> usize {
        if network.is_qbit() {
            crate::qbit_codec::WITNESS_SCALE_FACTOR
        } else {
            bitcoin::blockdata::constants::WITNESS_SCALE_FACTOR
        }
    }

    fn decode_pushnum(op: &All) -> Option<u8> {
        // 81 = OP_1, 96 = OP_16
        // 81 -> 1, so... 81 - 80 -> 1
        let self_u8 = op.into_u8();
        match self_u8 {
            81..=96 => Some(self_u8 - 80),
            _ => None,
        }
    }

    fn count_sigops(script: &script::Script, accurate: bool) -> usize {
        let mut n = 0;
        let mut pushnum_cache = None;
        for inst in script.instructions() {
            match inst {
                Ok(Instruction::Op(opcode)) => {
                    match opcode {
                        OP_CHECKSIG | OP_CHECKSIGVERIFY => {
                            n += 1;
                        }
                        OP_CHECKMULTISIG | OP_CHECKMULTISIGVERIFY => {
                            match (accurate, pushnum_cache) {
                                (true, Some(pushnum)) => {
                                    // Add the number of pubkeys in the multisig as sigop count
                                    n += usize::from(pushnum);
                                }
                                _ => {
                                    // MAX_PUBKEYS_PER_MULTISIG from Bitcoin Core
                                    // https://github.com/bitcoin/bitcoin/blob/v25.0/src/script/script.h#L29-L30
                                    n += 20;
                                }
                            }
                        }
                        _ => {}
                    }
                    // GetSigOpCount sets lastOpcode after every opcode, the sigop ones
                    // included, so an OP_N only counts for the opcode right after it
                    pushnum_cache = decode_pushnum(&opcode);
                }
                // We ignore errors as well as pushdatas
                _ => {
                    pushnum_cache = None;
                }
            }
        }

        n
    }

    /// Get the sigop count for legacy transactions
    fn get_legacy_sigop_count(tx: &Transaction) -> usize {
        let mut n = 0;
        for input in &tx.input {
            n += count_sigops(&input.script_sig, false);
        }
        for output in &tx.output {
            n += count_sigops(&output.script_pubkey, false);
        }
        n
    }

    fn get_p2sh_sigop_count(tx: &Transaction, previous_outputs: &[&TxOut]) -> usize {
        #[cfg(not(feature = "liquid"))]
        if tx.is_coin_base() {
            return 0;
        }
        #[cfg(feature = "liquid")]
        if tx.is_coinbase() {
            return 0;
        }
        let mut n = 0;
        for (input, prevout) in tx.input.iter().zip(previous_outputs.iter()) {
            if prevout.script_pubkey.is_p2sh() {
                if let Some(Ok(script::Instruction::PushBytes(redeem))) =
                    input.script_sig.instructions().last()
                {
                    let script =
                        script::Script::from_byte_iter(redeem.iter().map(|v| Ok(*v))).unwrap(); // I only return Ok, so it won't error
                    n += count_sigops(&script, true);
                }
            }
        }
        n
    }

    fn get_witness_sigop_count(tx: &Transaction, previous_outputs: &[&TxOut]) -> usize {
        let mut n = 0;

        #[inline]
        fn is_push_only(script: &script::Script) -> bool {
            for inst in script.instructions() {
                match inst {
                    Err(_) => return false,
                    Ok(Instruction::Op(_)) => return false,
                    Ok(Instruction::PushBytes(_)) => {}
                }
            }
            true
        }

        #[inline]
        fn last_pushdata(script: &script::Script) -> Option<&[u8]> {
            match script.instructions().last() {
                Some(Ok(Instruction::PushBytes(bytes))) => Some(bytes),
                _ => None,
            }
        }

        #[inline]
        fn count_with_prevout(
            prevout: &TxOut,
            script_sig: &script::Script,
            witness: &Witness,
        ) -> usize {
            let mut n = 0;

            let script = if prevout.script_pubkey.is_witness_program() {
                prevout.script_pubkey.clone()
            } else if prevout.script_pubkey.is_p2sh()
                && is_push_only(script_sig)
                && !script_sig.is_empty()
            {
                script::Script::from_byte_iter(
                    last_pushdata(script_sig).unwrap().iter().map(|v| Ok(*v)),
                )
                .unwrap()
            } else {
                return 0;
            };

            if script.is_v0_p2wsh() {
                let bytes = script.as_bytes();
                n += sig_ops(witness, bytes[0], &bytes[2..]);
            } else if script.is_v0_p2wpkh() {
                n += 1;
            }
            n
        }

        for (input, prevout) in tx.input.iter().zip(previous_outputs.iter()) {
            n += count_with_prevout(prevout, &input.script_sig, &input.witness);
        }
        n
    }

    /// Get the sigop cost for this transaction.
    fn get_sigop_cost(
        tx: &Transaction,
        previous_outputs: &[&TxOut],
        verify_p2sh: bool,
        verify_witness: bool,
        witness_scale_factor: usize,
    ) -> Result<usize, script::Error> {
        let mut n_sigop_cost = get_legacy_sigop_count(tx) * witness_scale_factor;
        #[cfg(not(feature = "liquid"))]
        if tx.is_coin_base() {
            return Ok(n_sigop_cost);
        }
        #[cfg(feature = "liquid")]
        if tx.is_coinbase() || tx.input.iter().any(|input| input.is_pegin) {
            return Ok(n_sigop_cost);
        }
        if tx.input.len() != previous_outputs.len() {
            return Err(script::Error::EarlyEndOfScript);
        }
        if verify_witness && !verify_p2sh {
            return Err(script::Error::EarlyEndOfScript);
        }
        if verify_p2sh {
            n_sigop_cost += get_p2sh_sigop_count(tx, previous_outputs) * witness_scale_factor;
        }
        if verify_witness {
            n_sigop_cost += get_witness_sigop_count(tx, previous_outputs);
        }

        Ok(n_sigop_cost)
    }

    /// Get sigops for the Witness
    ///
    /// witness_version is the raw opcode. OP_0 is 0, OP_1 is 81, etc.
    fn sig_ops(witness: &Witness, witness_version: u8, witness_program: &[u8]) -> usize {
        #[cfg(feature = "liquid")]
        let last_witness = witness.script_witness.last();
        #[cfg(not(feature = "liquid"))]
        let last_witness = witness.last();
        match (witness_version, witness_program.len()) {
            (0, 20) => 1,
            (0, 32) => last_witness
                .map(|sl| sl.iter().map(|v| Ok(*v)))
                .map(script::Script::from_byte_iter)
                // I only return Ok 2 lines up, so there is no way to error
                .map(|s| count_sigops(&s.unwrap(), true))
                .unwrap_or_default(),
            // qbit P2MR is witness v2 with a 32-byte program. It is bounded by
            // per-input validation weight in qbit consensus and contributes
            // zero to the legacy block-level witness sigop counter surfaced by
            // electrs' REST `sigops` field.
            (2, 32) => 0,
            _ => 0,
        }
    }

    #[cfg(all(test, not(feature = "liquid")))]
    mod qbit_tests {
        use super::*;
        use crate::chain::{OutPoint, Script, TxIn, Txid};

        fn txid() -> Txid {
            Txid::from_hex("0101010101010101010101010101010101010101010101010101010101010101")
                .unwrap()
        }

        #[test]
        fn p2mr_witness_contributes_zero_legacy_sigops() {
            let prevout = TxOut {
                value: 7,
                script_pubkey: Script::from(
                    hex::decode(
                        "52200000000000000000000000000000000000000000000000000000000000000000",
                    )
                    .unwrap(),
                ),
            };
            let outpoint = OutPoint {
                txid: txid(),
                vout: 0,
            };
            let mut leaf_script = vec![0x20];
            leaf_script.extend_from_slice(&[0x11; 32]);
            leaf_script.extend_from_slice(&[0xb3, 0xac]);
            let tx = Transaction {
                version: 2,
                lock_time: 0,
                input: vec![TxIn {
                    previous_output: outpoint,
                    script_sig: Script::new(),
                    sequence: 0xffff_ffff,
                    witness: Witness::from_vec(vec![vec![0x01], leaf_script, vec![0xc1]]),
                }],
                output: vec![],
            };
            let mut prevouts = HashMap::new();
            prevouts.insert(0, &prevout);

            assert_eq!(
                transaction_sigop_count(&tx, &prevouts, Network::QbitRegtest).unwrap(),
                0
            );
        }

        fn p2mr_script() -> Script {
            let mut script = vec![0x52, 0x20];
            script.extend_from_slice(&[0x00; 32]);
            Script::from(script)
        }

        #[test]
        fn qbit_counts_a_coinbase_legacy_sigop_once() {
            // a height push, then an OP_CHECKMULTISIG byte, as in mainnet coinbase
            // 9f62a6f8c688f7b23a5813771a49c161b905b61f3c1554e97b6b5ee04897bb29. qbitd's
            // GetLegacySigOpCount gives 20, and a coinbase's sigop cost is that times
            // WITNESS_SCALE_FACTOR, 1 on qbit.
            let coinbase = Transaction {
                version: 2,
                lock_time: 0,
                input: vec![TxIn {
                    previous_output: OutPoint::null(),
                    script_sig: Script::from(vec![0x03, 0x2a, 0x00, 0x00, 0xae]),
                    sequence: 0xffff_ffff,
                    witness: Witness::default(),
                }],
                output: vec![TxOut {
                    value: 21_000_000_000,
                    script_pubkey: p2mr_script(),
                }],
            };
            let no_prevouts = HashMap::new();

            for network in [Network::Qbit, Network::QbitTestnet4, Network::QbitRegtest] {
                assert_eq!(
                    transaction_sigop_count(&coinbase, &no_prevouts, network).unwrap(),
                    20,
                    "{:?}",
                    network
                );
            }
            // Bitcoin's WITNESS_SCALE_FACTOR is 4
            assert_eq!(
                transaction_sigop_count(&coinbase, &no_prevouts, Network::Bitcoin).unwrap(),
                80
            );
        }

        #[test]
        fn qbit_counts_legacy_and_p2sh_sigops_once_and_witness_sigops_as_before() {
            // P2SH (on qbit regtest with -p2mronly=0) spending a 1-of-1 multisig redeem
            // script: 1 P2SH sigop
            let mut redeem_script = vec![0x51, 0x21];
            redeem_script.extend_from_slice(&[0x02; 33]);
            redeem_script.extend_from_slice(&[0x51, 0xae]);
            let mut p2sh_script_sig = vec![0x00, 0x01, 0x30, redeem_script.len() as u8];
            p2sh_script_sig.extend_from_slice(&redeem_script);
            let mut p2sh = vec![0xa9, 0x14];
            p2sh.extend_from_slice(&[0x22; 20]);
            p2sh.push(0x87);
            let p2sh_prevout = TxOut {
                value: 7,
                script_pubkey: Script::from(p2sh),
            };
            // P2WSH with a <key> OP_CHECKSIG witness script: 1 witness sigop
            let mut witness_script = vec![0x21];
            witness_script.extend_from_slice(&[0x03; 33]);
            witness_script.push(0xac);
            let mut p2wsh = vec![0x00, 0x20];
            p2wsh.extend_from_slice(&[0x33; 32]);
            let p2wsh_prevout = TxOut {
                value: 7,
                script_pubkey: Script::from(p2wsh),
            };
            let tx = Transaction {
                version: 2,
                lock_time: 0,
                input: vec![
                    TxIn {
                        previous_output: OutPoint {
                            txid: txid(),
                            vout: 0,
                        },
                        script_sig: Script::from(p2sh_script_sig),
                        sequence: 0xffff_ffff,
                        witness: Witness::default(),
                    },
                    TxIn {
                        previous_output: OutPoint {
                            txid: txid(),
                            vout: 1,
                        },
                        script_sig: Script::new(),
                        sequence: 0xffff_ffff,
                        witness: Witness::from_vec(vec![vec![0x30], witness_script]),
                    },
                ],
                // OP_RETURN OP_CHECKSIG: 1 legacy sigop
                output: vec![TxOut {
                    value: 0,
                    script_pubkey: Script::from(vec![0x6a, 0xac]),
                }],
            };
            let mut prevouts = HashMap::new();
            prevouts.insert(0, &p2sh_prevout);
            prevouts.insert(1, &p2wsh_prevout);

            // qbitd's GetTransactionSigOpCost at a WITNESS_SCALE_FACTOR of 1: 1 + 1 + 1
            assert_eq!(
                transaction_sigop_count(&tx, &prevouts, Network::QbitRegtest).unwrap(),
                3
            );
            // and Bitcoin's at 4: 4 * 1 + 4 * 1 + 1
            assert_eq!(
                transaction_sigop_count(&tx, &prevouts, Network::Bitcoin).unwrap(),
                9
            );
        }

        #[test]
        fn an_op_n_counts_only_for_the_opcode_right_after_it() {
            // qbitd's GetSigOpCount, counting accurately: an OP_CHECKMULTISIG right after
            // OP_1 to OP_16 counts N, and any other one 20
            let count =
                |bytes: &[u8], accurate| count_sigops(&Script::from(bytes.to_vec()), accurate);
            // OP_2 OP_CHECKMULTISIG
            assert_eq!(count(&[0x52, 0xae], true), 2);
            // OP_2 OP_CHECKMULTISIG OP_CHECKMULTISIG
            assert_eq!(count(&[0x52, 0xae, 0xae], true), 2 + 20);
            // OP_2 OP_CHECKSIG OP_CHECKMULTISIG
            assert_eq!(count(&[0x52, 0xac, 0xae], true), 1 + 20);
            // OP_2 OP_CHECKSIGVERIFY OP_CHECKMULTISIGVERIFY
            assert_eq!(count(&[0x52, 0xad, 0xaf], true), 1 + 20);
            // counting inaccurately, every OP_CHECKMULTISIG is 20
            assert_eq!(count(&[0x52, 0xae, 0xae], false), 20 + 20);
        }

        #[test]
        fn witness_sigops_count_a_stale_op_n_as_20() {
            // a P2WSH spend whose witness script is OP_2 OP_CHECKMULTISIG OP_CHECKMULTISIG
            let witness_script = vec![0x52, 0xae, 0xae];
            let mut p2wsh = vec![0x00, 0x20];
            p2wsh.extend_from_slice(&[0x33; 32]);
            let prevout = TxOut {
                value: 7,
                script_pubkey: Script::from(p2wsh),
            };
            let tx = Transaction {
                version: 2,
                lock_time: 0,
                input: vec![TxIn {
                    previous_output: OutPoint {
                        txid: txid(),
                        vout: 0,
                    },
                    script_sig: Script::new(),
                    sequence: 0xffff_ffff,
                    witness: Witness::from_vec(vec![vec![], witness_script]),
                }],
                output: vec![],
            };
            let mut prevouts = HashMap::new();
            prevouts.insert(0, &prevout);

            assert_eq!(
                transaction_sigop_count(&tx, &prevouts, Network::QbitRegtest).unwrap(),
                2 + 20
            );
        }
    }
}
