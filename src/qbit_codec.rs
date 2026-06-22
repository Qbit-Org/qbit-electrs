use bitcoin::consensus::encode::{self, deserialize, serialize, Decodable};
use bitcoin::hash_types::TxMerkleNode;
use bitcoin::{BlockHeader, Transaction, TxIn, TxOut};

use std::fmt;
use std::io::Cursor;

pub const WITNESS_SCALE_FACTOR: usize = 1;
pub const BLOCK_VERSION_TOP_BITS: i32 = 0x2000_0000;
pub const BLOCK_VERSION_TOP_MASK: i32 = 0xE000_0000u32 as i32;
pub const AUXPOW_VERSION_FLAG: i32 = 0x0000_0100;
pub const PURE_BLOCK_HEADER_LEN: usize = 80;

pub type MerkleBranchNode = TxMerkleNode;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuxPow {
    pub coinbase_tx: Transaction,
    pub coinbase_merkle_branch: Vec<MerkleBranchNode>,
    pub coinbase_branch_index: i32,
    pub chain_merkle_branch: Vec<MerkleBranchNode>,
    pub chain_index: i32,
    pub parent_block_header: BlockHeader,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Block {
    pub header: BlockHeader,
    pub auxpow: Option<AuxPow>,
    pub txdata: Vec<Transaction>,
    serialized_size: usize,
}

impl Block {
    pub fn has_auxpow(&self) -> bool {
        self.auxpow.is_some()
    }

    pub fn serialized_size(&self) -> usize {
        self.serialized_size
    }

    pub fn weight(&self) -> usize {
        self.serialized_size
    }
}

impl From<Block> for bitcoin::Block {
    fn from(block: Block) -> Self {
        bitcoin::Block {
            header: block.header,
            txdata: block.txdata,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    HeaderLength { actual: usize },
    ShortBlockHeader { actual: usize },
    Consensus(String),
    TrailingBytes { consumed: usize, total: usize },
}

impl From<encode::Error> for Error {
    fn from(err: encode::Error) -> Self {
        Error::Consensus(err.to_string())
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::HeaderLength { actual } => write!(
                f,
                "qbit pure block header must be {} bytes, got {}",
                PURE_BLOCK_HEADER_LEN, actual
            ),
            Error::ShortBlockHeader { actual } => write!(
                f,
                "qbit block must start with a {}-byte pure header, got {} bytes",
                PURE_BLOCK_HEADER_LEN, actual
            ),
            Error::Consensus(err) => write!(f, "qbit consensus decode error: {}", err),
            Error::TrailingBytes { consumed, total } => write!(
                f,
                "qbit decode consumed {} bytes but input has {} bytes",
                consumed, total
            ),
        }
    }
}

impl std::error::Error for Error {}

pub fn has_bip9_top_bits_shape(version: i32) -> bool {
    (version & BLOCK_VERSION_TOP_MASK) == BLOCK_VERSION_TOP_BITS
}

pub fn has_auxpow_flag(version: i32) -> bool {
    has_bip9_top_bits_shape(version) && (version & AUXPOW_VERSION_FLAG) != 0
}

pub fn deserialize_pure_header(bytes: &[u8]) -> Result<BlockHeader, Error> {
    if bytes.len() != PURE_BLOCK_HEADER_LEN {
        return Err(Error::HeaderLength {
            actual: bytes.len(),
        });
    }
    deserialize(bytes).map_err(Error::from)
}

pub fn deserialize_transaction(bytes: &[u8]) -> Result<Transaction, Error> {
    let mut cursor = Cursor::new(bytes);
    let tx = Transaction::consensus_decode(&mut cursor)?;
    ensure_fully_consumed(&cursor, bytes.len())?;
    Ok(tx)
}

pub fn deserialize_header(bytes: &[u8]) -> Result<BlockHeader, Error> {
    if bytes.len() < PURE_BLOCK_HEADER_LEN {
        return Err(Error::ShortBlockHeader {
            actual: bytes.len(),
        });
    }

    let header = deserialize_pure_header(&bytes[..PURE_BLOCK_HEADER_LEN])?;
    let mut cursor = Cursor::new(bytes);
    cursor.set_position(PURE_BLOCK_HEADER_LEN as u64);

    if has_auxpow_flag(header.version) {
        deserialize_auxpow(&mut cursor)?;
    }

    ensure_fully_consumed(&cursor, bytes.len())?;
    Ok(header)
}

pub fn transaction_size(tx: &Transaction) -> usize {
    serialize(tx).len()
}

pub fn transaction_weight(tx: &Transaction) -> usize {
    transaction_size(tx)
}

pub fn deserialize_block(bytes: &[u8]) -> Result<Block, Error> {
    if bytes.len() < PURE_BLOCK_HEADER_LEN {
        return Err(Error::ShortBlockHeader {
            actual: bytes.len(),
        });
    }

    let header = deserialize_pure_header(&bytes[..PURE_BLOCK_HEADER_LEN])?;
    let mut cursor = Cursor::new(bytes);
    cursor.set_position(PURE_BLOCK_HEADER_LEN as u64);

    let auxpow = if has_auxpow_flag(header.version) {
        Some(deserialize_auxpow(&mut cursor)?)
    } else {
        None
    };

    let txdata = Vec::<Transaction>::consensus_decode(&mut cursor)?;
    ensure_fully_consumed(&cursor, bytes.len())?;

    Ok(Block {
        header,
        auxpow,
        txdata,
        serialized_size: bytes.len(),
    })
}

pub fn deserialize_block_as_bitcoin(bytes: &[u8]) -> Result<bitcoin::Block, Error> {
    deserialize_block(bytes).map(Into::into)
}

fn deserialize_auxpow(cursor: &mut Cursor<&[u8]>) -> Result<AuxPow, Error> {
    Ok(AuxPow {
        coinbase_tx: deserialize_no_witness_transaction(&mut *cursor)?,
        coinbase_merkle_branch: Vec::<MerkleBranchNode>::consensus_decode(&mut *cursor)?,
        coinbase_branch_index: i32::consensus_decode(&mut *cursor)?,
        chain_merkle_branch: Vec::<MerkleBranchNode>::consensus_decode(&mut *cursor)?,
        chain_index: i32::consensus_decode(&mut *cursor)?,
        parent_block_header: BlockHeader::consensus_decode(cursor)?,
    })
}

fn deserialize_no_witness_transaction(
    cursor: &mut Cursor<&[u8]>,
) -> Result<Transaction, encode::Error> {
    Ok(Transaction {
        version: i32::consensus_decode(&mut *cursor)?,
        input: Vec::<TxIn>::consensus_decode(&mut *cursor)?,
        output: Vec::<TxOut>::consensus_decode(&mut *cursor)?,
        lock_time: u32::consensus_decode(cursor)?,
    })
}

fn ensure_fully_consumed(cursor: &Cursor<&[u8]>, total: usize) -> Result<(), Error> {
    let consumed = cursor.position() as usize;
    if consumed == total {
        Ok(())
    } else {
        Err(Error::TrailingBytes { consumed, total })
    }
}
