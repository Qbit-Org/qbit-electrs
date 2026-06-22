use crate::chain::{BlockHash, BlockHeader};
use crate::errors::*;
use crate::new_index::BlockEntry;

use std::collections::HashMap;
use std::fmt;
use std::iter::FromIterator;
use std::slice;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime as DateTime;

const MTP_SPAN: usize = 11;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct BlockId {
    pub height: usize,
    pub hash: BlockHash,
    pub time: u32,
}

impl From<&HeaderEntry> for BlockId {
    fn from(header: &HeaderEntry) -> Self {
        BlockId {
            height: header.height(),
            hash: *header.hash(),
            time: header.header().time,
        }
    }
}

#[derive(Eq, PartialEq, Clone)]
pub struct HeaderEntry {
    height: usize,
    hash: BlockHash,
    header: BlockHeader,
}

impl HeaderEntry {
    pub fn hash(&self) -> &BlockHash {
        &self.hash
    }

    pub fn header(&self) -> &BlockHeader {
        &self.header
    }

    pub fn height(&self) -> usize {
        self.height
    }
}

impl fmt::Debug for HeaderEntry {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        let last_block_time = DateTime::from_unix_timestamp(self.header().time as i64).unwrap();
        write!(
            f,
            "hash={} height={} @ {}",
            self.hash(),
            self.height(),
            last_block_time.format(&Rfc3339).unwrap(),
        )
    }
}

pub struct HeaderList {
    headers: Vec<HeaderEntry>,
    heights: HashMap<BlockHash, usize>,
    tip: BlockHash,
}

impl HeaderList {
    pub fn empty() -> HeaderList {
        HeaderList {
            headers: vec![],
            heights: HashMap::new(),
            tip: BlockHash::default(),
        }
    }

    pub fn new(
        mut headers_map: HashMap<BlockHash, BlockHeader>,
        tip_hash: BlockHash,
    ) -> HeaderList {
        trace!(
            "processing {} headers, tip at {:?}",
            headers_map.len(),
            tip_hash
        );

        let mut blockhash = tip_hash;
        let mut headers_chain: Vec<BlockHeader> = vec![];
        let null_hash = BlockHash::default();

        while blockhash != null_hash {
            let header = headers_map.remove(&blockhash).unwrap_or_else(|| {
                panic!(
                    "missing expected blockhash in headers map: {:?}, pointed from: {:?}",
                    blockhash,
                    headers_chain.last().map(|h| h.block_hash())
                )
            });
            blockhash = header.prev_blockhash;
            headers_chain.push(header);
        }
        headers_chain.reverse();

        trace!(
            "{} chained headers ({} orphan blocks left)",
            headers_chain.len(),
            headers_map.len()
        );

        let mut headers = HeaderList::empty();
        headers.apply(headers.order(headers_chain));
        headers
    }

    pub fn order(&self, new_headers: Vec<BlockHeader>) -> Vec<HeaderEntry> {
        // header[i] -> header[i-1] (i.e. header.last() is the tip)
        struct HashedHeader {
            blockhash: BlockHash,
            header: BlockHeader,
        }
        let hashed_headers =
            Vec::<HashedHeader>::from_iter(new_headers.into_iter().map(|header| HashedHeader {
                blockhash: header.block_hash(),
                header,
            }));
        for i in 1..hashed_headers.len() {
            assert_eq!(
                hashed_headers[i].header.prev_blockhash,
                hashed_headers[i - 1].blockhash
            );
        }
        let prev_blockhash = match hashed_headers.first() {
            Some(h) => h.header.prev_blockhash,
            None => return vec![], // hashed_headers is empty
        };
        let null_hash = BlockHash::default();
        let new_height: usize = if prev_blockhash == null_hash {
            0
        } else {
            self.header_by_blockhash(&prev_blockhash)
                .unwrap_or_else(|| panic!("{} is not part of the blockchain", prev_blockhash))
                .height()
                + 1
        };
        (new_height..)
            .zip(hashed_headers)
            .map(|(height, hashed_header)| HeaderEntry {
                height,
                hash: hashed_header.blockhash,
                header: hashed_header.header,
            })
            .collect()
    }

    /// Returns any rolled back blocks in order from old tip first and first block in the fork is last
    /// It also returns the blockhash of the post-rollback tip.
    pub fn apply(
        &mut self,
        new_headers: Vec<HeaderEntry>,
    ) -> (Vec<HeaderEntry>, Option<BlockHash>) {
        // new_headers[i] -> new_headers[i - 1] (i.e. new_headers.last() is the tip)
        for i in 1..new_headers.len() {
            assert_eq!(new_headers[i - 1].height() + 1, new_headers[i].height());
            assert_eq!(
                *new_headers[i - 1].hash(),
                new_headers[i].header().prev_blockhash
            );
        }
        let new_height = match new_headers.first() {
            Some(entry) => {
                let height = entry.height();
                let expected_prev_blockhash = if height > 0 {
                    *self.headers[height - 1].hash()
                } else {
                    BlockHash::default()
                };
                assert_eq!(entry.header().prev_blockhash, expected_prev_blockhash);
                height
            }
            None => return (vec![], None),
        };
        debug!(
            "applying {} new headers from height {}",
            new_headers.len(),
            new_height
        );
        let mut removed = self.headers.split_off(new_height); // keep [0..new_height) entries

        // If we reorged, we should return the last blockhash before adding the new chain's blockheaders.
        let reorged_tip = if !removed.is_empty() {
            self.headers.last().map(|be| be.hash()).cloned()
        } else {
            None
        };

        for new_header in new_headers {
            let height = new_header.height();
            assert_eq!(height, self.headers.len());
            self.tip = *new_header.hash();
            self.headers.push(new_header);
            self.heights.insert(self.tip, height);
        }
        removed.reverse();
        (removed, reorged_tip)
    }

    pub fn rollback_to(
        &mut self,
        tip: &BlockHash,
    ) -> Option<(Vec<HeaderEntry>, Option<BlockHash>)> {
        let new_len = self.header_by_blockhash(tip)?.height() + 1;
        let mut removed = self.headers.split_off(new_len);
        if removed.is_empty() {
            return Some((vec![], None));
        }

        for header in &removed {
            self.heights.remove(header.hash());
        }
        self.tip = *tip;
        removed.reverse();
        Some((removed, Some(*tip)))
    }

    pub fn header_by_blockhash(&self, blockhash: &BlockHash) -> Option<&HeaderEntry> {
        let height = self.heights.get(blockhash)?;
        let header = self.headers.get(*height)?;
        if *blockhash == *header.hash() {
            Some(header)
        } else {
            None
        }
    }

    pub fn header_by_height(&self, height: usize) -> Option<&HeaderEntry> {
        self.headers.get(height).inspect(|entry| {
            assert_eq!(entry.height(), height);
        })
    }

    pub fn equals(&self, other: &HeaderList) -> bool {
        self.headers.last() == other.headers.last()
    }

    pub fn tip(&self) -> &BlockHash {
        assert_eq!(
            self.tip,
            self.headers.last().map(|h| *h.hash()).unwrap_or_default()
        );
        &self.tip
    }

    pub fn len(&self) -> usize {
        self.headers.len()
    }

    pub fn is_empty(&self) -> bool {
        self.headers.is_empty()
    }

    pub fn iter(&self) -> slice::Iter<'_, HeaderEntry> {
        self.headers.iter()
    }

    /// Get the Median Time Past
    pub fn get_mtp(&self, height: usize) -> u32 {
        // Use the timestamp as the mtp of the genesis block.
        // Matches bitcoind's behaviour: bitcoin-cli getblock `bitcoin-cli getblockhash 0` | jq '.time == .mediantime'
        if height == 0 {
            self.headers.first().unwrap().header.time
        } else if height > self.len() - 1 {
            0
        } else {
            let mut timestamps = (height.saturating_sub(MTP_SPAN - 1)..=height)
                .map(|p_height| self.headers.get(p_height).unwrap().header.time)
                .collect::<Vec<_>>();
            timestamps.sort_unstable();
            timestamps[timestamps.len() / 2]
        }
    }
}

#[derive(Serialize, Deserialize)]
pub struct BlockStatus {
    pub in_best_chain: bool,
    pub height: Option<usize>,
    pub next_best: Option<BlockHash>,
}

impl BlockStatus {
    pub fn confirmed(height: usize, next_best: Option<BlockHash>) -> BlockStatus {
        BlockStatus {
            in_best_chain: true,
            height: Some(height),
            next_best,
        }
    }

    pub fn orphaned() -> BlockStatus {
        BlockStatus {
            in_best_chain: false,
            height: None,
            next_best: None,
        }
    }
}

#[derive(Serialize, Deserialize, Debug)]
pub struct BlockMeta {
    #[serde(alias = "nTx")]
    pub tx_count: u32,
    pub size: u32,
    pub weight: u32,
}

pub struct BlockHeaderMeta {
    pub header_entry: HeaderEntry,
    pub meta: BlockMeta,
    pub mtp: u32,
}

impl From<&BlockEntry> for BlockMeta {
    fn from(b: &BlockEntry) -> BlockMeta {
        BlockMeta {
            tx_count: b.block.txdata.len() as u32,
            weight: b.weight,
            size: b.size,
        }
    }
}

impl BlockMeta {
    pub fn parse_getblock(val: ::serde_json::Value) -> Result<BlockMeta> {
        Ok(BlockMeta {
            tx_count: val
                .get("nTx")
                .chain_err(|| "missing nTx")?
                .as_f64()
                .chain_err(|| "nTx not a number")? as u32,
            size: val
                .get("size")
                .chain_err(|| "missing size")?
                .as_f64()
                .chain_err(|| "size not a number")? as u32,
            weight: val
                .get("weight")
                .chain_err(|| "missing weight")?
                .as_f64()
                .chain_err(|| "weight not a number")? as u32,
        })
    }
}

#[cfg(all(test, not(feature = "liquid")))]
mod tests {
    use super::*;
    use crate::chain::Block;
    use crate::qbit_codec;

    fn header(prev_blockhash: BlockHash, nonce: u32) -> BlockHeader {
        BlockHeader {
            version: 1,
            prev_blockhash,
            merkle_root: "0000000000000000000000000000000000000000000000000000000000000000"
                .parse()
                .unwrap(),
            time: nonce,
            bits: 0,
            nonce,
        }
    }

    fn fixture_hex(path: &str) -> Vec<u8> {
        let hex = std::fs::read_to_string(path).expect("fixture hex should be readable");
        hex::decode(hex.split_whitespace().collect::<String>()).expect("fixture hex should decode")
    }

    fn header_list_from(headers: Vec<BlockHeader>) -> HeaderList {
        let mut list = HeaderList::empty();
        let ordered = list.order(headers);
        list.apply(ordered);
        list
    }

    #[test]
    fn header_list_rolls_back_to_indexed_ancestor() {
        let genesis = header(BlockHash::default(), 0);
        let child = header(genesis.block_hash(), 1);
        let tip = header(child.block_hash(), 2);
        let child_hash = child.block_hash();
        let tip_hash = tip.block_hash();

        let mut headers = header_list_from(vec![genesis, child, tip]);

        let (removed, rollback_tip) = headers
            .rollback_to(&child_hash)
            .expect("child should be an indexed ancestor");

        assert_eq!(rollback_tip, Some(child_hash));
        assert_eq!(removed.len(), 1);
        assert_eq!(*removed[0].hash(), tip_hash);
        assert_eq!(*headers.tip(), child_hash);
        assert_eq!(headers.len(), 2);
        assert!(headers.header_by_blockhash(&tip_hash).is_none());
    }

    #[test]
    fn header_list_rolls_back_multiple_blocks_in_old_tip_order() {
        let genesis = header(BlockHash::default(), 0);
        let child = header(genesis.block_hash(), 1);
        let grandchild = header(child.block_hash(), 2);
        let tip = header(grandchild.block_hash(), 3);
        let child_hash = child.block_hash();
        let grandchild_hash = grandchild.block_hash();
        let tip_hash = tip.block_hash();

        let mut headers = header_list_from(vec![genesis, child, grandchild, tip]);

        let (removed, rollback_tip) = headers
            .rollback_to(&child_hash)
            .expect("child should be an indexed ancestor");

        assert_eq!(rollback_tip, Some(child_hash));
        assert_eq!(
            removed
                .iter()
                .map(|entry| *entry.hash())
                .collect::<Vec<_>>(),
            vec![tip_hash, grandchild_hash]
        );
        assert_eq!(*headers.tip(), child_hash);
        assert_eq!(headers.len(), 2);
        assert!(headers.header_by_blockhash(&tip_hash).is_none());
        assert!(headers.header_by_blockhash(&grandchild_hash).is_none());
    }

    #[test]
    fn header_list_rolls_back_to_genesis() {
        let genesis = header(BlockHash::default(), 0);
        let child = header(genesis.block_hash(), 1);
        let tip = header(child.block_hash(), 2);
        let genesis_hash = genesis.block_hash();
        let child_hash = child.block_hash();
        let tip_hash = tip.block_hash();

        let mut headers = header_list_from(vec![genesis, child, tip]);

        let (removed, rollback_tip) = headers
            .rollback_to(&genesis_hash)
            .expect("genesis should be indexed");

        assert_eq!(rollback_tip, Some(genesis_hash));
        assert_eq!(
            removed
                .iter()
                .map(|entry| *entry.hash())
                .collect::<Vec<_>>(),
            vec![tip_hash, child_hash]
        );
        assert_eq!(*headers.tip(), genesis_hash);
        assert_eq!(headers.len(), 1);
    }

    #[test]
    fn header_list_rollback_to_current_tip_is_noop() {
        let genesis = header(BlockHash::default(), 0);
        let tip = header(genesis.block_hash(), 1);
        let tip_hash = tip.block_hash();

        let mut headers = header_list_from(vec![genesis, tip]);

        let (removed, rollback_tip) = headers
            .rollback_to(&tip_hash)
            .expect("tip should be indexed");

        assert!(removed.is_empty());
        assert_eq!(rollback_tip, None);
        assert_eq!(*headers.tip(), tip_hash);
        assert_eq!(headers.len(), 2);
    }

    #[test]
    fn header_list_rollback_to_unknown_hash_is_none() {
        let genesis = header(BlockHash::default(), 0);
        let tip = header(genesis.block_hash(), 1);
        let tip_hash = tip.block_hash();
        let unknown_hash = header(tip_hash, 2).block_hash();

        let mut headers = header_list_from(vec![genesis, tip]);

        assert!(headers.rollback_to(&unknown_hash).is_none());
        assert_eq!(*headers.tip(), tip_hash);
        assert_eq!(headers.len(), 2);
    }

    #[test]
    fn block_meta_uses_fetcher_supplied_weight() {
        let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-qbitd-auxpow-block.hex");
        let block: Block = qbit_codec::deserialize_block_as_bitcoin(&raw)
            .expect("qbit AuxPoW block should parse")
            .into();
        let entry = HeaderEntry {
            height: 0,
            hash: block.block_hash(),
            header: block.header,
        };
        let block_entry = BlockEntry {
            weight: raw.len() as u32,
            size: raw.len() as u32,
            block,
            entry,
        };

        let meta = BlockMeta::from(&block_entry);
        assert_eq!(meta.size, raw.len() as u32);
        assert_eq!(meta.weight, raw.len() as u32);
        assert_ne!(meta.weight, block_entry.block.weight() as u32);
    }
}
