#![cfg(not(feature = "liquid"))]

use std::convert::TryInto;

use bech32::{u5, ToBase32, Variant};
use electrs::chain::{Network, Script};
use electrs::new_index::compute_script_hash;
use electrs::qbit_address;
use serde_json::Value;

fn fixture(name: &str) -> Value {
    let text = match name {
        "mainnet" => include_str!("fixtures/qbit/addresses/mainnet-p2mr-zero-program.json"),
        "testnet4" => include_str!("fixtures/qbit/addresses/testnet4-p2mr-zero-program.json"),
        "regtest" => include_str!("fixtures/qbit/addresses/regtest-p2mr-zero-program.json"),
        _ => panic!("unknown fixture path"),
    };
    serde_json::from_str(text).expect("address fixture should parse")
}

fn network(name: &str) -> Network {
    match name {
        "mainnet" => Network::Qbit,
        "testnet4" => Network::QbitTestnet4,
        "regtest" => Network::QbitRegtest,
        _ => panic!("unknown qbit network"),
    }
}

#[test]
fn qbit_p2mr_fixtures_round_trip() {
    for name in ["mainnet", "testnet4", "regtest"] {
        let fixture = fixture(name);
        let network = network(name);
        let p2mr = &fixture["p2mr"];
        let address = p2mr["address"]
            .as_str()
            .expect("address should be a string");
        let script = Script::from(
            hex::decode(
                p2mr["script_pubkey_hex"]
                    .as_str()
                    .expect("script_pubkey_hex should be a string"),
            )
            .expect("script_pubkey_hex should decode"),
        );
        let program: [u8; 32] = hex::decode(
            p2mr["witness_program_hex"]
                .as_str()
                .expect("witness_program_hex should be a string"),
        )
        .expect("witness_program_hex should decode")
        .try_into()
        .expect("witness program should be 32 bytes");

        assert_eq!(
            qbit_address::p2mr_program_from_script(&script),
            Some(program)
        );
        assert_eq!(
            qbit_address::encode_p2mr_address(&program, network).unwrap(),
            address
        );
        assert_eq!(
            qbit_address::script_to_address(&script, network).unwrap(),
            address
        );
        assert_eq!(
            qbit_address::decode_p2mr_address(address, network).unwrap(),
            script
        );
        assert_eq!(
            qbit_address::script_pubkey_from_address(address, network).unwrap(),
            script
        );
    }
}

#[test]
fn qbit_p2mr_rejects_wrong_networks_and_bitcoin_hrps() {
    for name in ["mainnet", "testnet4", "regtest"] {
        let fixture = fixture(name);
        let network = network(name);
        for vector in fixture["wrong_network_rejection_vectors"]
            .as_array()
            .expect("wrong-network vectors should be an array")
        {
            let address = vector["address"]
                .as_str()
                .expect("wrong-network address should be a string");
            assert!(qbit_address::decode_p2mr_address(address, network).is_err());
        }
    }

    let zero_program = [0u8; 32];
    let mut data = vec![u5::try_from_u8(2).unwrap()];
    data.extend_from_slice(&zero_program.to_base32());
    let bitcoin_hrp_address = bech32::encode("bc", data, Variant::Bech32m).unwrap();
    assert!(qbit_address::decode_p2mr_address(&bitcoin_hrp_address, Network::Qbit).is_err());
}

#[test]
fn qbit_p2mr_scripthash_matches_fixture_script_bytes() {
    let fixture = fixture("regtest");
    let script = Script::from(
        hex::decode(
            fixture["p2mr"]["script_pubkey_hex"]
                .as_str()
                .expect("script_pubkey_hex should be a string"),
        )
        .expect("script_pubkey_hex should decode"),
    );

    assert_eq!(
        hex::encode(compute_script_hash(&script)),
        "379bfa9706fc3e3b0dbe80ff6615a46c781ca7124daf93e513c0f77f5bb12257"
    );
}
