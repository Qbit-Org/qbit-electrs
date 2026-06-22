use std::{convert::TryInto, fmt};

use bech32::{u5, FromBase32, ToBase32, Variant};
use bitcoin::util::base58;

use crate::chain::{Network, Script};

const OP_2: u8 = 0x52;
const OP_DUP: u8 = 0x76;
const OP_HASH160: u8 = 0xa9;
const OP_EQUAL: u8 = 0x87;
const OP_EQUALVERIFY: u8 = 0x88;
const OP_CHECKSIG: u8 = 0xac;
const OP_PUSHBYTES_20: u8 = 0x14;
const OP_PUSHBYTES_32: u8 = 0x20;

pub const P2MR_WITNESS_VERSION: u8 = 2;
pub const P2MR_WITNESS_PROGRAM_LEN: usize = 32;
pub const P2MR_SCRIPT_LEN: usize = 34;
const HASH160_LEN: usize = 20;
const BASE58_PAYLOAD_LEN: usize = 1 + HASH160_LEN;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    UnsupportedNetwork(Network),
    InvalidBech32(String),
    InvalidBech32Variant(Variant),
    MissingWitnessVersion,
    InvalidHrp {
        expected: &'static str,
        actual: String,
    },
    InvalidWitnessVersion(u8),
    InvalidWitnessProgramLength(usize),
    InvalidBase58(String),
    InvalidBase58PayloadLength(usize),
    InvalidBase58Prefix(u8),
    UnsupportedScript,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::UnsupportedNetwork(network) => {
                write!(f, "qbit addresses are not supported on {:?}", network)
            }
            Error::InvalidBech32(err) => write!(f, "invalid qbit Bech32m address: {}", err),
            Error::InvalidBech32Variant(variant) => {
                write!(f, "qbit P2MR address must use Bech32m, got {:?}", variant)
            }
            Error::MissingWitnessVersion => write!(f, "qbit address is missing witness version"),
            Error::InvalidHrp { expected, actual } => write!(
                f,
                "invalid qbit address HRP: expected {}, got {}",
                expected, actual
            ),
            Error::InvalidWitnessVersion(version) => write!(
                f,
                "invalid qbit P2MR witness version: expected {}, got {}",
                P2MR_WITNESS_VERSION, version
            ),
            Error::InvalidWitnessProgramLength(len) => write!(
                f,
                "invalid qbit P2MR witness program length: expected {} bytes, got {}",
                P2MR_WITNESS_PROGRAM_LEN, len
            ),
            Error::InvalidBase58(err) => write!(f, "invalid qbit Base58 address: {}", err),
            Error::InvalidBase58PayloadLength(len) => write!(
                f,
                "invalid qbit Base58 payload length: expected {} bytes, got {}",
                BASE58_PAYLOAD_LEN, len
            ),
            Error::InvalidBase58Prefix(prefix) => {
                write!(f, "invalid qbit Base58 address prefix: {}", prefix)
            }
            Error::UnsupportedScript => write!(f, "script has no qbit address encoding"),
        }
    }
}

impl std::error::Error for Error {}

pub fn p2mr_program_from_script(script: &Script) -> Option<[u8; P2MR_WITNESS_PROGRAM_LEN]> {
    let bytes = script.as_bytes();
    if bytes.len() != P2MR_SCRIPT_LEN || bytes[0] != OP_2 || bytes[1] != OP_PUSHBYTES_32 {
        return None;
    }
    bytes[2..].try_into().ok()
}

pub fn encode_p2mr_address(
    program: &[u8; P2MR_WITNESS_PROGRAM_LEN],
    network: Network,
) -> Result<String, Error> {
    let hrp = network
        .qbit_bech32_hrp()
        .ok_or(Error::UnsupportedNetwork(network))?;
    let mut data = vec![u5::try_from_u8(P2MR_WITNESS_VERSION).expect("valid witness version")];
    data.extend_from_slice(&program.to_base32());
    bech32::encode(hrp, data, Variant::Bech32m).map_err(|err| Error::InvalidBech32(err.to_string()))
}

pub fn decode_p2mr_address(addr: &str, network: Network) -> Result<Script, Error> {
    let expected_hrp = network
        .qbit_bech32_hrp()
        .ok_or(Error::UnsupportedNetwork(network))?;
    let (actual_hrp, data, variant) =
        bech32::decode(addr).map_err(|err| Error::InvalidBech32(err.to_string()))?;

    if actual_hrp != expected_hrp {
        return Err(Error::InvalidHrp {
            expected: expected_hrp,
            actual: actual_hrp,
        });
    }
    if variant != Variant::Bech32m {
        return Err(Error::InvalidBech32Variant(variant));
    }

    let witness_version = data
        .first()
        .copied()
        .ok_or(Error::MissingWitnessVersion)?
        .to_u8();
    if witness_version != P2MR_WITNESS_VERSION {
        return Err(Error::InvalidWitnessVersion(witness_version));
    }

    let program =
        Vec::<u8>::from_base32(&data[1..]).map_err(|err| Error::InvalidBech32(err.to_string()))?;
    if program.len() != P2MR_WITNESS_PROGRAM_LEN {
        return Err(Error::InvalidWitnessProgramLength(program.len()));
    }

    Ok(p2mr_script(&program.try_into().expect("checked length")))
}

pub fn script_pubkey_from_address(addr: &str, network: Network) -> Result<Script, Error> {
    let expected_hrp = network
        .qbit_bech32_hrp()
        .ok_or(Error::UnsupportedNetwork(network))?;
    let lower = addr.to_ascii_lowercase();
    let expected_qbit_bech32 = lower.len() > expected_hrp.len()
        && lower.as_bytes()[expected_hrp.len()] == b'1'
        && &lower[..expected_hrp.len()] == expected_hrp;

    if expected_qbit_bech32 || bech32::decode(addr).is_ok() {
        return decode_p2mr_address(addr, network);
    }
    decode_base58_address(addr, network)
}

pub fn script_to_address(script: &Script, network: Network) -> Option<String> {
    if let Some(program) = p2mr_program_from_script(script) {
        return encode_p2mr_address(&program, network).ok();
    }
    encode_base58_address(script, network).ok()
}

fn p2mr_script(program: &[u8; P2MR_WITNESS_PROGRAM_LEN]) -> Script {
    let mut bytes = Vec::with_capacity(P2MR_SCRIPT_LEN);
    bytes.push(OP_2);
    bytes.push(OP_PUSHBYTES_32);
    bytes.extend_from_slice(program);
    Script::from(bytes)
}

fn p2pkh_script(hash: &[u8; HASH160_LEN]) -> Script {
    let mut bytes = Vec::with_capacity(25);
    bytes.extend_from_slice(&[OP_DUP, OP_HASH160, OP_PUSHBYTES_20]);
    bytes.extend_from_slice(hash);
    bytes.extend_from_slice(&[OP_EQUALVERIFY, OP_CHECKSIG]);
    Script::from(bytes)
}

fn p2sh_script(hash: &[u8; HASH160_LEN]) -> Script {
    let mut bytes = Vec::with_capacity(23);
    bytes.extend_from_slice(&[OP_HASH160, OP_PUSHBYTES_20]);
    bytes.extend_from_slice(hash);
    bytes.push(OP_EQUAL);
    Script::from(bytes)
}

fn p2pkh_hash_from_script(script: &Script) -> Option<[u8; HASH160_LEN]> {
    let bytes = script.as_bytes();
    if bytes.len() != 25
        || bytes[0] != OP_DUP
        || bytes[1] != OP_HASH160
        || bytes[2] != OP_PUSHBYTES_20
        || bytes[23] != OP_EQUALVERIFY
        || bytes[24] != OP_CHECKSIG
    {
        return None;
    }
    bytes[3..23].try_into().ok()
}

fn p2sh_hash_from_script(script: &Script) -> Option<[u8; HASH160_LEN]> {
    let bytes = script.as_bytes();
    if bytes.len() != 23
        || bytes[0] != OP_HASH160
        || bytes[1] != OP_PUSHBYTES_20
        || bytes[22] != OP_EQUAL
    {
        return None;
    }
    bytes[2..22].try_into().ok()
}

#[derive(Debug, Copy, Clone)]
struct Base58Prefixes {
    p2pkh: u8,
    p2sh: u8,
}

fn base58_prefixes(network: Network) -> Option<Base58Prefixes> {
    match network {
        Network::Qbit => Some(Base58Prefixes {
            p2pkh: 58,
            p2sh: 63,
        }),
        Network::QbitTestnet4 | Network::QbitRegtest => Some(Base58Prefixes {
            p2pkh: 120,
            p2sh: 125,
        }),
        _ => None,
    }
}

fn encode_base58_address(script: &Script, network: Network) -> Result<String, Error> {
    let prefixes = base58_prefixes(network).ok_or(Error::UnsupportedNetwork(network))?;
    let (prefix, hash) = if let Some(hash) = p2pkh_hash_from_script(script) {
        (prefixes.p2pkh, hash)
    } else if let Some(hash) = p2sh_hash_from_script(script) {
        (prefixes.p2sh, hash)
    } else {
        return Err(Error::UnsupportedScript);
    };
    let mut payload = Vec::with_capacity(BASE58_PAYLOAD_LEN);
    payload.push(prefix);
    payload.extend_from_slice(&hash);
    Ok(base58::check_encode_slice(&payload))
}

fn decode_base58_address(addr: &str, network: Network) -> Result<Script, Error> {
    let prefixes = base58_prefixes(network).ok_or(Error::UnsupportedNetwork(network))?;
    let payload = base58::from_check(addr).map_err(|err| Error::InvalidBase58(err.to_string()))?;
    if payload.len() != BASE58_PAYLOAD_LEN {
        return Err(Error::InvalidBase58PayloadLength(payload.len()));
    }
    let hash: [u8; HASH160_LEN] = payload[1..]
        .try_into()
        .expect("checked Base58 payload length");
    match payload[0] {
        prefix if prefix == prefixes.p2pkh => Ok(p2pkh_script(&hash)),
        prefix if prefix == prefixes.p2sh => Ok(p2sh_script(&hash)),
        prefix => Err(Error::InvalidBase58Prefix(prefix)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const OP_0: u8 = 0x00;

    #[test]
    fn rejects_non_p2mr_witness_program_lengths() {
        let short_script = Script::from(vec![OP_2, 0x02, 0xaa, 0xbb]);
        assert_eq!(p2mr_program_from_script(&short_script), None);

        let mut long_script = vec![OP_2, 0x21];
        long_script.extend_from_slice(&[0u8; 33]);
        assert_eq!(p2mr_program_from_script(&Script::from(long_script)), None);
    }

    #[test]
    fn qbit_base58_round_trips_legacy_scripts() {
        let p2pkh = Script::from(
            hex::decode("76a914000000000000000000000000000000000000000088ac").unwrap(),
        );
        let p2sh =
            Script::from(hex::decode("a914000000000000000000000000000000000000000087").unwrap());

        let p2pkh_addr = script_to_address(&p2pkh, Network::QbitRegtest).unwrap();
        let p2sh_addr = script_to_address(&p2sh, Network::QbitRegtest).unwrap();

        assert!(p2pkh_addr.starts_with('q'));
        assert!(p2sh_addr.starts_with('s'));
        assert_eq!(
            script_pubkey_from_address(&p2pkh_addr, Network::QbitRegtest).unwrap(),
            p2pkh
        );
        assert_eq!(
            script_pubkey_from_address(&p2sh_addr, Network::QbitRegtest).unwrap(),
            p2sh
        );
    }

    #[test]
    fn qbit_base58_rejects_wrong_network_prefix() {
        let p2pkh = Script::from(
            hex::decode("76a914000000000000000000000000000000000000000088ac").unwrap(),
        );
        let mainnet_addr = script_to_address(&p2pkh, Network::Qbit).unwrap();

        assert!(matches!(
            script_pubkey_from_address(&mainnet_addr, Network::QbitTestnet4),
            Err(Error::InvalidBase58Prefix(58))
        ));
    }

    #[test]
    fn v0_witness_scripts_do_not_encode_as_qbit_addresses() {
        let mut bytes = vec![OP_0, OP_PUSHBYTES_20];
        bytes.extend_from_slice(&[0u8; HASH160_LEN]);
        assert_eq!(script_to_address(&Script::from(bytes), Network::Qbit), None);
    }
}
