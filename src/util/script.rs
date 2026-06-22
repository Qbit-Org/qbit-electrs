#[cfg(feature = "liquid")]
use elements::address as elements_address;

use crate::chain::{script, Network, Script, TxIn, TxOut};
use script::Instruction::PushBytes;

pub struct InnerScripts {
    pub redeem_script: Option<Script>,
    pub witness_script: Option<Script>,
}

pub trait ScriptToAsm: std::fmt::Debug {
    fn to_asm(&self) -> String {
        let asm = format!("{:?}", self);
        asm[7..asm.len() - 1].to_string()
    }
}
impl ScriptToAsm for bitcoin::Script {}
#[cfg(feature = "liquid")]
impl ScriptToAsm for elements::Script {}

pub fn to_asm_for_network<T: ScriptToAsm + ?Sized>(script: &T, network: Network) -> String {
    let asm = script.to_asm();
    if network.is_qbit() {
        asm.replace("OP_NOP4", "OP_CHECKSIGPQC")
            .replace("OP_RETURN_187", "OP_CHECKTEMPLATEVERIFY")
            .replace("OP_RETURN_188", "OP_CHECKDATASIGPQC")
            .replace("OP_RETURN_189", "OP_CHECKDATASIGADDPQC")
    } else {
        asm
    }
}

pub trait ScriptToAddr {
    fn to_address_str(&self, network: Network) -> Option<String>;
}
#[cfg(not(feature = "liquid"))]
impl ScriptToAddr for bitcoin::Script {
    fn to_address_str(&self, network: Network) -> Option<String> {
        if network.is_qbit() {
            return crate::qbit_address::script_to_address(self, network);
        }
        bitcoin::Address::from_script(self, network.bitcoin_network()?).map(|s| s.to_string())
    }
}
#[cfg(feature = "liquid")]
impl ScriptToAddr for elements::Script {
    fn to_address_str(&self, network: Network) -> Option<String> {
        elements_address::Address::from_script(self, None, network.address_params())
            .map(|a| a.to_string())
    }
}

// Returns the witnessScript in the case of p2wsh, or the redeemScript in the case of p2sh.
pub fn get_innerscripts(txin: &TxIn, prevout: &TxOut, network: Network) -> InnerScripts {
    #[cfg(feature = "liquid")]
    let _ = network;

    // Wrapped redeemScript for P2SH spends
    let redeem_script = if prevout.script_pubkey.is_p2sh() {
        if let Some(Ok(PushBytes(redeemscript))) = txin.script_sig.instructions().last() {
            Some(Script::from(redeemscript.to_vec()))
        } else {
            None
        }
    } else {
        None
    };

    #[cfg(not(feature = "liquid"))]
    let is_qbit_p2mr = network.is_qbit()
        && crate::qbit_address::p2mr_program_from_script(&prevout.script_pubkey).is_some();
    #[cfg(feature = "liquid")]
    let is_qbit_p2mr = false;

    // Wrapped witnessScript for P2WSH/P2SH-P2WSH, Taproot script path, or qbit P2MR script path spends.
    let witness_script = if prevout.script_pubkey.is_v0_p2wsh()
        || prevout.script_pubkey.is_v1_p2tr()
        || is_qbit_p2mr
        || redeem_script.as_ref().is_some_and(|s| s.is_v0_p2wsh())
    {
        let witness = &txin.witness;
        #[cfg(feature = "liquid")]
        let witness = &witness.script_witness;

        // rust-bitcoin returns witness items as a [u8] slice, while rust-elements returns a Vec<u8>
        #[cfg(not(feature = "liquid"))]
        let wit_to_vec = Vec::from;
        #[cfg(feature = "liquid")]
        let wit_to_vec = Clone::clone;

        let inner_script_slice = if is_qbit_p2mr {
            let w_len = witness.len();
            let annex_items = witness
                .last()
                .filter(|last_elem| last_elem.first().filter(|&&v| v == 0x50).is_some())
                .map_or(0, |_| 1);
            let control_pos_from_last = annex_items + 1;
            let script_pos_from_last = control_pos_from_last + 1;

            if w_len >= script_pos_from_last {
                let control_index = w_len - control_pos_from_last;
                let script_index = w_len - script_pos_from_last;

                #[allow(clippy::iter_nth)]
                let control = witness.iter().nth(control_index);
                #[allow(clippy::iter_nth)]
                let script = witness.iter().nth(script_index);

                match control {
                    Some(control) if is_valid_p2mr_control_block(control) => script,
                    _ => None,
                }
            } else {
                None
            }
        } else if prevout.script_pubkey.is_v1_p2tr() {
            // Witness stack is potentially very large
            // so we avoid to_vec() or iter().collect() for performance
            let w_len = witness.len();
            witness
                .last()
                // Get the position of the script spend script (if it exists)
                .map(|last_elem| {
                    // From BIP341:
                    // If there are at least two witness elements, and the first byte of
                    // the last element is 0x50, this last element is called annex a
                    // and is removed from the witness stack.
                    if w_len >= 2 && last_elem.first().filter(|&&v| v == 0x50).is_some() {
                        // account for the extra item removed from the end
                        3
                    } else {
                        // otherwise script is 2nd from last
                        2
                    }
                })
                // Convert to None if not script spend
                // Note: Option doesn't have filter_map() method
                .filter(|&script_pos_from_last| w_len >= script_pos_from_last)
                .and_then(|script_pos_from_last| {
                    // Can't use second_to_last() since it might be 3rd to last
                    #[allow(clippy::iter_nth)]
                    witness.iter().nth(w_len - script_pos_from_last)
                })
        } else {
            witness.last()
        };

        inner_script_slice.map(wit_to_vec).map(Script::from)
    } else {
        None
    };

    InnerScripts {
        redeem_script,
        witness_script,
    }
}

fn is_valid_p2mr_control_block(control: &[u8]) -> bool {
    const P2MR_CONTROL_BASE_SIZE: usize = 1;
    const P2MR_CONTROL_NODE_SIZE: usize = 32;
    const P2MR_CONTROL_MAX_NODE_COUNT: usize = 128;
    const P2MR_CONTROL_MAX_SIZE: usize =
        P2MR_CONTROL_BASE_SIZE + P2MR_CONTROL_NODE_SIZE * P2MR_CONTROL_MAX_NODE_COUNT;

    control.len() >= P2MR_CONTROL_BASE_SIZE
        && control.len() <= P2MR_CONTROL_MAX_SIZE
        && (control.len() - P2MR_CONTROL_BASE_SIZE) % P2MR_CONTROL_NODE_SIZE == 0
        && control[0] & 1 == 1
}

#[cfg(all(test, not(feature = "liquid")))]
mod qbit_tests {
    use super::*;
    use bitcoin::{OutPoint, Witness};

    fn p2mr_prevout() -> TxOut {
        TxOut {
            value: 7,
            script_pubkey: Script::from(
                hex::decode("52200000000000000000000000000000000000000000000000000000000000000000")
                    .unwrap(),
            ),
        }
    }

    fn p2mr_leaf_script() -> Vec<u8> {
        let mut script = vec![0x20];
        script.extend_from_slice(&[0x11; 32]);
        script.push(0xb3);
        script
    }

    fn p2mr_control_block(branch_nodes: usize) -> Vec<u8> {
        let mut control = vec![0xc1];
        control.extend(std::iter::repeat(0x22).take(32 * branch_nodes));
        control
    }

    fn txin_with_witness(witness: Vec<Vec<u8>>) -> TxIn {
        TxIn {
            previous_output: OutPoint::null(),
            script_sig: Script::new(),
            sequence: 0xffff_ffff,
            witness: Witness::from_vec(witness),
        }
    }

    #[test]
    fn qbit_asm_uses_qbit_opcode_names() {
        let script = Script::from(
            hex::decode(
                "20aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab3bbbcbd",
            )
            .unwrap(),
        );
        let asm = to_asm_for_network(&script, Network::QbitRegtest);

        assert!(asm.contains("OP_CHECKSIGPQC"));
        assert!(asm.contains("OP_CHECKTEMPLATEVERIFY"));
        assert!(asm.contains("OP_CHECKDATASIGPQC"));
        assert!(asm.contains("OP_CHECKDATASIGADDPQC"));
        assert!(!asm.contains("OP_NOP4"));
        assert!(!asm.contains("OP_RETURN_187"));
    }

    #[test]
    fn p2mr_witness_leaf_script_extracts_without_annex() {
        let leaf_script = p2mr_leaf_script();
        let txin = txin_with_witness(vec![vec![0x01], leaf_script.clone(), p2mr_control_block(0)]);
        let innerscripts = get_innerscripts(&txin, &p2mr_prevout(), Network::QbitRegtest);

        assert_eq!(innerscripts.witness_script, Some(Script::from(leaf_script)));
    }

    #[test]
    fn p2mr_witness_leaf_script_extracts_with_annex() {
        let leaf_script = p2mr_leaf_script();
        let txin = txin_with_witness(vec![
            vec![0x01],
            leaf_script.clone(),
            p2mr_control_block(1),
            vec![0x50, 0x01],
        ]);
        let innerscripts = get_innerscripts(&txin, &p2mr_prevout(), Network::QbitRegtest);

        assert_eq!(innerscripts.witness_script, Some(Script::from(leaf_script)));
    }

    #[test]
    fn p2mr_witness_leaf_script_rejects_malformed_control_block() {
        let txin = txin_with_witness(vec![vec![0x01], p2mr_leaf_script(), vec![0xc1, 0x00]]);
        let innerscripts = get_innerscripts(&txin, &p2mr_prevout(), Network::QbitRegtest);

        assert_eq!(innerscripts.witness_script, None);
    }

    #[test]
    fn p2mr_witness_leaf_script_does_not_extract_on_bitcoin_networks() {
        let txin = txin_with_witness(vec![vec![0x01], p2mr_leaf_script(), p2mr_control_block(0)]);
        let innerscripts = get_innerscripts(&txin, &p2mr_prevout(), Network::Bitcoin);

        assert_eq!(innerscripts.witness_script, None);
    }
}
