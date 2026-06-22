use std::str::FromStr;

#[cfg(not(feature = "liquid"))] // use regular Bitcoin data structures
pub use bitcoin::{
    blockdata::{opcodes, script, witness::Witness},
    consensus::deserialize,
    hashes,
    util::address,
    Block, BlockHash, BlockHeader, OutPoint, Script, Transaction, TxIn, TxOut, Txid,
};

#[cfg(feature = "liquid")]
pub use {
    crate::elements::asset,
    elements::{
        address, confidential, encode::deserialize, hashes, opcodes, script, Address, AssetId,
        Block, BlockHash, BlockHeader, OutPoint, Script, Transaction, TxIn, TxInWitness as Witness,
        TxOut, Txid,
    },
};

use bitcoin::blockdata::constants::genesis_block;
pub use bitcoin::network::constants::Network as BNetwork;

#[cfg(not(feature = "liquid"))]
pub type Value = u64;
#[cfg(feature = "liquid")]
pub use confidential::Value;

#[derive(Debug, Copy, Clone, PartialEq, Hash, Serialize, Ord, PartialOrd, Eq)]
pub enum Network {
    #[cfg(not(feature = "liquid"))]
    Bitcoin,
    #[cfg(not(feature = "liquid"))]
    Testnet,
    #[cfg(not(feature = "liquid"))]
    Testnet4,
    #[cfg(not(feature = "liquid"))]
    Regtest,
    #[cfg(not(feature = "liquid"))]
    Signet,
    #[cfg(not(feature = "liquid"))]
    Qbit,
    #[cfg(not(feature = "liquid"))]
    QbitTestnet4,
    #[cfg(not(feature = "liquid"))]
    QbitRegtest,

    #[cfg(feature = "liquid")]
    Liquid,
    #[cfg(feature = "liquid")]
    LiquidTestnet,
    #[cfg(feature = "liquid")]
    LiquidRegtest,
}

#[cfg(feature = "liquid")]
pub const LIQUID_TESTNET_PARAMS: address::AddressParams = address::AddressParams {
    p2pkh_prefix: 36,
    p2sh_prefix: 19,
    blinded_prefix: 23,
    bech_hrp: "tex",
    blech_hrp: "tlq",
};

/// Magic for testnet4, 0x1c163f28 (from BIP94) with flipped endianness.
#[cfg(not(feature = "liquid"))]
const TESTNET4_MAGIC: u32 = 0x283f161c;

#[cfg(not(feature = "liquid"))]
pub const QBIT_MAINNET_MAGIC: u32 = 0xA824_4F44;
#[cfg(not(feature = "liquid"))]
pub const QBIT_TESTNET4_MAGIC: u32 = 0x4016_C4C7;
#[cfg(not(feature = "liquid"))]
pub const QBIT_REGTEST_MAGIC: u32 = 0xDA1F_6BA6;

#[cfg(not(feature = "liquid"))]
pub const QBIT_MAINNET_HRP: &str = "qb";
#[cfg(not(feature = "liquid"))]
pub const QBIT_TESTNET4_HRP: &str = "tq";
#[cfg(not(feature = "liquid"))]
pub const QBIT_REGTEST_HRP: &str = "qbrt";

impl Network {
    #[cfg(not(feature = "liquid"))]
    pub fn magic(self) -> u32 {
        match self {
            Self::Testnet4 => TESTNET4_MAGIC,
            Self::Qbit => QBIT_MAINNET_MAGIC,
            Self::QbitTestnet4 => QBIT_TESTNET4_MAGIC,
            Self::QbitRegtest => QBIT_REGTEST_MAGIC,
            _ => self
                .bitcoin_network()
                .expect("qbit network has no rust-bitcoin network magic")
                .magic(),
        }
    }

    #[cfg(feature = "liquid")]
    pub fn magic(self) -> u32 {
        match self {
            Network::Liquid | Network::LiquidRegtest => 0xDAB5_BFFA,
            Network::LiquidTestnet => 0x62DD_0E41,
        }
    }

    pub fn is_regtest(self) -> bool {
        match self {
            #[cfg(not(feature = "liquid"))]
            Network::Regtest => true,
            #[cfg(not(feature = "liquid"))]
            Network::QbitRegtest => true,
            #[cfg(feature = "liquid")]
            Network::LiquidRegtest => true,
            _ => false,
        }
    }

    #[cfg(not(feature = "liquid"))]
    pub fn is_qbit(self) -> bool {
        matches!(
            self,
            Network::Qbit | Network::QbitTestnet4 | Network::QbitRegtest
        )
    }

    #[cfg(feature = "liquid")]
    pub fn is_qbit(self) -> bool {
        false
    }

    #[cfg(not(feature = "liquid"))]
    pub fn qbit_bech32_hrp(self) -> Option<&'static str> {
        match self {
            Network::Qbit => Some(QBIT_MAINNET_HRP),
            Network::QbitTestnet4 => Some(QBIT_TESTNET4_HRP),
            Network::QbitRegtest => Some(QBIT_REGTEST_HRP),
            _ => None,
        }
    }

    #[cfg(feature = "liquid")]
    pub fn qbit_bech32_hrp(self) -> Option<&'static str> {
        None
    }

    #[cfg(not(feature = "liquid"))]
    pub fn bitcoin_network(self) -> Option<BNetwork> {
        match self {
            Network::Bitcoin => Some(BNetwork::Bitcoin),
            Network::Testnet => Some(BNetwork::Testnet),
            Network::Testnet4 => Some(BNetwork::Testnet),
            Network::Regtest => Some(BNetwork::Regtest),
            Network::Signet => Some(BNetwork::Signet),
            Network::Qbit | Network::QbitTestnet4 | Network::QbitRegtest => None,
        }
    }

    pub fn canonical_name(self) -> &'static str {
        match self {
            #[cfg(not(feature = "liquid"))]
            Network::Bitcoin => "mainnet",
            #[cfg(not(feature = "liquid"))]
            Network::Testnet => "testnet",
            #[cfg(not(feature = "liquid"))]
            Network::Testnet4 => "testnet4",
            #[cfg(not(feature = "liquid"))]
            Network::Regtest => "regtest",
            #[cfg(not(feature = "liquid"))]
            Network::Signet => "signet",
            #[cfg(not(feature = "liquid"))]
            Network::Qbit => "qbit",
            #[cfg(not(feature = "liquid"))]
            Network::QbitTestnet4 => "qbittestnet4",
            #[cfg(not(feature = "liquid"))]
            Network::QbitRegtest => "qbitregtest",

            #[cfg(feature = "liquid")]
            Network::Liquid => "liquid",
            #[cfg(feature = "liquid")]
            Network::LiquidTestnet => "liquidtestnet",
            #[cfg(feature = "liquid")]
            Network::LiquidRegtest => "liquidregtest",
        }
    }

    pub fn daemon_chain_name(self) -> &'static str {
        match self {
            #[cfg(not(feature = "liquid"))]
            Network::Bitcoin | Network::Qbit => "main",
            #[cfg(not(feature = "liquid"))]
            Network::Testnet => "test",
            #[cfg(not(feature = "liquid"))]
            Network::Testnet4 | Network::QbitTestnet4 => "testnet4",
            #[cfg(not(feature = "liquid"))]
            Network::Regtest | Network::QbitRegtest => "regtest",
            #[cfg(not(feature = "liquid"))]
            Network::Signet => "signet",

            #[cfg(feature = "liquid")]
            Network::Liquid => "liquidv1",
            #[cfg(feature = "liquid")]
            Network::LiquidTestnet => "liquidtestnet",
            #[cfg(feature = "liquid")]
            Network::LiquidRegtest => "liquidregtest",
        }
    }

    #[cfg(not(feature = "liquid"))]
    pub fn has_static_genesis_hash(self) -> bool {
        true
    }

    #[cfg(feature = "liquid")]
    pub fn has_static_genesis_hash(self) -> bool {
        matches!(self, Network::Liquid)
    }

    #[cfg(feature = "liquid")]
    pub fn address_params(self) -> &'static address::AddressParams {
        // Liquid regtest uses elements's address params
        match self {
            Network::Liquid => &address::AddressParams::LIQUID,
            Network::LiquidRegtest => &address::AddressParams::ELEMENTS,
            Network::LiquidTestnet => &LIQUID_TESTNET_PARAMS,
        }
    }

    #[cfg(feature = "liquid")]
    pub fn native_asset(self) -> &'static AssetId {
        match self {
            Network::Liquid => &asset::NATIVE_ASSET_ID,
            Network::LiquidTestnet => &asset::NATIVE_ASSET_ID_TESTNET,
            Network::LiquidRegtest => &asset::NATIVE_ASSET_ID_REGTEST,
        }
    }

    #[cfg(feature = "liquid")]
    pub fn pegged_asset(self) -> Option<&'static AssetId> {
        match self {
            Network::Liquid => Some(&*asset::NATIVE_ASSET_ID),
            Network::LiquidTestnet | Network::LiquidRegtest => None,
        }
    }

    pub fn names() -> Vec<String> {
        #[cfg(not(feature = "liquid"))]
        return vec![
            "mainnet".to_string(),
            "testnet".to_string(),
            "testnet4".to_string(),
            "regtest".to_string(),
            "signet".to_string(),
            "qbit".to_string(),
            "qbittestnet4".to_string(),
            "qbitregtest".to_string(),
        ];

        #[cfg(feature = "liquid")]
        return vec![
            "liquid".to_string(),
            "liquidtestnet".to_string(),
            "liquidregtest".to_string(),
        ];
    }
}

pub fn genesis_hash(network: Network) -> BlockHash {
    #[cfg(not(feature = "liquid"))]
    return bitcoin_genesis_hash(network);
    #[cfg(feature = "liquid")]
    return liquid_genesis_hash(network);
}

pub fn bitcoin_genesis_hash(network: Network) -> bitcoin::BlockHash {
    lazy_static! {
        static ref BITCOIN_GENESIS: bitcoin::BlockHash =
            genesis_block(BNetwork::Bitcoin).block_hash();
        static ref TESTNET_GENESIS: bitcoin::BlockHash =
            genesis_block(BNetwork::Testnet).block_hash();
        static ref TESTNET4_GENESIS: bitcoin::BlockHash = bitcoin::BlockHash::from_str(
            "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043"
        )
        .unwrap();
        static ref REGTEST_GENESIS: bitcoin::BlockHash =
            genesis_block(BNetwork::Regtest).block_hash();
        static ref SIGNET_GENESIS: bitcoin::BlockHash =
            genesis_block(BNetwork::Signet).block_hash();
        static ref QBIT_MAINNET_GENESIS: bitcoin::BlockHash = bitcoin::BlockHash::from_str(
            "0000324188278d089b5eabd9b62bf874c7512677cea90720af51ea5a61a2f997"
        )
        .unwrap();
        static ref QBIT_TESTNET4_GENESIS: bitcoin::BlockHash = bitcoin::BlockHash::from_str(
            "000000000000796fe86bbc0bf1b66a07e4b4c0676f74b54cf7e5ce8b3f1a0090"
        )
        .unwrap();
        static ref QBIT_REGTEST_GENESIS: bitcoin::BlockHash = bitcoin::BlockHash::from_str(
            "0ee96aa77c4b600850e349344fa21b107e805f5370ddc7a6189db12cf69acce6"
        )
        .unwrap();
    }
    #[cfg(not(feature = "liquid"))]
    match network {
        Network::Bitcoin => *BITCOIN_GENESIS,
        Network::Testnet => *TESTNET_GENESIS,
        Network::Testnet4 => *TESTNET4_GENESIS,
        Network::Regtest => *REGTEST_GENESIS,
        Network::Signet => *SIGNET_GENESIS,
        Network::Qbit => *QBIT_MAINNET_GENESIS,
        Network::QbitTestnet4 => *QBIT_TESTNET4_GENESIS,
        Network::QbitRegtest => *QBIT_REGTEST_GENESIS,
    }
    #[cfg(feature = "liquid")]
    match network {
        Network::Liquid => *BITCOIN_GENESIS,
        Network::LiquidTestnet => *TESTNET_GENESIS,
        Network::LiquidRegtest => *REGTEST_GENESIS,
    }
}

#[cfg(feature = "liquid")]
pub fn liquid_genesis_hash(network: Network) -> elements::BlockHash {
    lazy_static! {
        static ref LIQUID_GENESIS: BlockHash =
            "1466275836220db2944ca059a3a10ef6fd2ea684b0688d2c379296888a206003"
                .parse()
                .unwrap();
    }

    match network {
        Network::Liquid => *LIQUID_GENESIS,
        // The genesis block for liquid regtest chains varies based on the chain configuration.
        // This instead uses an all zeroed-out hash, which doesn't matter in practice because its
        // only used for Electrum server discovery, which isn't active on regtest.
        _ => Default::default(),
    }
}

impl From<&str> for Network {
    fn from(network_name: &str) -> Self {
        match network_name {
            #[cfg(not(feature = "liquid"))]
            "mainnet" => Network::Bitcoin,
            #[cfg(not(feature = "liquid"))]
            "testnet" => Network::Testnet,
            #[cfg(not(feature = "liquid"))]
            "testnet4" => Network::Testnet4,
            #[cfg(not(feature = "liquid"))]
            "regtest" => Network::Regtest,
            #[cfg(not(feature = "liquid"))]
            "signet" => Network::Signet,
            #[cfg(not(feature = "liquid"))]
            "qbit" | "qbitmainnet" | "qbit-mainnet" => Network::Qbit,
            #[cfg(not(feature = "liquid"))]
            "qbittestnet4" | "qbit-testnet4" => Network::QbitTestnet4,
            #[cfg(not(feature = "liquid"))]
            "qbitregtest" | "qbit-regtest" => Network::QbitRegtest,

            #[cfg(feature = "liquid")]
            "liquid" => Network::Liquid,
            #[cfg(feature = "liquid")]
            "liquidtestnet" => Network::LiquidTestnet,
            #[cfg(feature = "liquid")]
            "liquidregtest" => Network::LiquidRegtest,

            _ => panic!("unsupported network: {:?}", network_name),
        }
    }
}

#[cfg(not(feature = "liquid"))]
impl From<Network> for BNetwork {
    fn from(network: Network) -> Self {
        network
            .bitcoin_network()
            .expect("qbit network cannot be represented as rust-bitcoin Network")
    }
}

#[cfg(not(feature = "liquid"))]
impl From<BNetwork> for Network {
    fn from(network: BNetwork) -> Self {
        match network {
            BNetwork::Bitcoin => Network::Bitcoin,
            BNetwork::Testnet => Network::Testnet,
            BNetwork::Regtest => Network::Regtest,
            BNetwork::Signet => Network::Signet,
        }
    }
}

#[cfg(all(test, not(feature = "liquid")))]
mod tests {
    use super::*;

    #[test]
    fn qbit_network_contract_constants_match_doc() {
        assert_eq!(Network::Qbit.magic(), QBIT_MAINNET_MAGIC);
        assert_eq!(Network::QbitTestnet4.magic(), QBIT_TESTNET4_MAGIC);
        assert_eq!(Network::QbitRegtest.magic(), QBIT_REGTEST_MAGIC);

        assert_eq!(Network::Qbit.qbit_bech32_hrp(), Some("qb"));
        assert_eq!(Network::QbitTestnet4.qbit_bech32_hrp(), Some("tq"));
        assert_eq!(Network::QbitRegtest.qbit_bech32_hrp(), Some("qbrt"));

        assert_eq!(
            genesis_hash(Network::Qbit).to_string(),
            "0000324188278d089b5eabd9b62bf874c7512677cea90720af51ea5a61a2f997"
        );
        assert_eq!(
            genesis_hash(Network::QbitTestnet4).to_string(),
            "000000000000796fe86bbc0bf1b66a07e4b4c0676f74b54cf7e5ce8b3f1a0090"
        );
        assert_eq!(
            genesis_hash(Network::QbitRegtest).to_string(),
            "0ee96aa77c4b600850e349344fa21b107e805f5370ddc7a6189db12cf69acce6"
        );
    }

    #[test]
    fn qbit_network_names_parse_as_distinct_variants() {
        assert_eq!(Network::from("qbit"), Network::Qbit);
        assert_eq!(Network::from("qbit-mainnet"), Network::Qbit);
        assert_eq!(Network::from("qbittestnet4"), Network::QbitTestnet4);
        assert_eq!(Network::from("qbit-testnet4"), Network::QbitTestnet4);
        assert_eq!(Network::from("qbitregtest"), Network::QbitRegtest);
        assert_eq!(Network::from("qbit-regtest"), Network::QbitRegtest);

        assert!(Network::Qbit.is_qbit());
        assert!(Network::QbitTestnet4.is_qbit());
        assert!(Network::QbitRegtest.is_qbit());
        assert!(!Network::Testnet4.is_qbit());
        assert!(Network::QbitRegtest.is_regtest());
    }
}
