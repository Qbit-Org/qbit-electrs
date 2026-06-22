use rayon::prelude::*;

#[cfg(not(feature = "liquid"))]
use bitcoin::consensus::encode::Decodable;
#[cfg(feature = "liquid")]
use elements::encode::Decodable;

use std::collections::HashMap;
use std::fs;
use std::io::Cursor;
use std::path::PathBuf;
use std::thread;

use crate::chain::{Block, BlockHash, Network};
use crate::daemon::{qbit_or_default_block_from_bytes, Daemon};
use crate::errors::*;
use crate::util::{spawn_thread, HeaderEntry, SyncChannel};

const DEFAULT_BLOCK_BATCH_SIZE: usize = 100;
// qbit blocks can carry AuxPoW payloads and large PQC witness data; keep RPC
// block batches conservative until the integration harness tunes this with
// live testnet data.
const QBIT_BLOCK_BATCH_SIZE: usize = 10;

#[derive(Clone, Copy, Debug)]
pub enum FetchFrom {
    Bitcoind,
    BlkFiles,
}

pub fn start_fetcher(
    from: FetchFrom,
    daemon: &Daemon,
    new_headers: Vec<HeaderEntry>,
) -> Result<Fetcher<Vec<BlockEntry>>> {
    let fetcher = match from {
        FetchFrom::Bitcoind => bitcoind_fetcher,
        FetchFrom::BlkFiles => blkfiles_fetcher,
    };
    fetcher(daemon, new_headers)
}

pub struct BlockEntry {
    pub block: Block,
    pub entry: HeaderEntry,
    pub size: u32,
    pub weight: u32,
}

type SizedBlock = (Block, u32);

fn block_batch_size(network: Network) -> usize {
    if network.is_qbit() {
        QBIT_BLOCK_BATCH_SIZE
    } else {
        DEFAULT_BLOCK_BATCH_SIZE
    }
}

fn block_weight(network: Network, block: &Block, raw_size: u32) -> u32 {
    if network.is_qbit() {
        raw_size
    } else {
        block.weight() as u32
    }
}

pub struct SequentialFetcher<T> {
    fetcher: Box<dyn FnOnce() -> Vec<Vec<T>>>,
}

impl<T> SequentialFetcher<T> {
    fn from<F: FnOnce() -> Vec<Vec<T>> + 'static>(pre_func: F) -> Self {
        SequentialFetcher {
            fetcher: Box::new(pre_func),
        }
    }

    pub fn map<FN>(self, mut func: FN)
    where
        FN: FnMut(Vec<T>),
    {
        for item in (self.fetcher)() {
            func(item);
        }
    }
}

pub fn bitcoind_sequential_fetcher(
    daemon: &Daemon,
    new_headers: Vec<HeaderEntry>,
) -> Result<SequentialFetcher<BlockEntry>> {
    let daemon = daemon.reconnect()?;
    let batch_size = block_batch_size(daemon.network());
    Ok(SequentialFetcher::from(move || {
        new_headers
            .chunks(batch_size)
            .map(|entries| {
                let blockhashes: Vec<BlockHash> = entries.iter().map(|e| *e.hash()).collect();
                let sized_blocks = daemon
                    .getblocks_with_size(&blockhashes)
                    .expect("failed to get blocks from bitcoind");
                assert_eq!(sized_blocks.len(), entries.len());
                let block_entries: Vec<BlockEntry> = sized_blocks
                    .into_iter()
                    .zip(entries)
                    .map(|((block, size), entry)| BlockEntry {
                        entry: entry.clone(), // TODO: remove this clone()
                        weight: block_weight(daemon.network(), &block, size),
                        size,
                        block,
                    })
                    .collect();
                assert_eq!(block_entries.len(), entries.len());
                block_entries
            })
            .collect()
    }))
}

pub struct Fetcher<T> {
    receiver: crossbeam_channel::Receiver<T>,
    thread: thread::JoinHandle<()>,
}

impl<T> Fetcher<T> {
    fn from(receiver: crossbeam_channel::Receiver<T>, thread: thread::JoinHandle<()>) -> Self {
        Fetcher { receiver, thread }
    }

    pub fn map<F>(self, mut func: F)
    where
        F: FnMut(T),
    {
        for item in self.receiver {
            func(item);
        }
        self.thread.join().expect("fetcher thread panicked")
    }
}

fn bitcoind_fetcher(
    daemon: &Daemon,
    new_headers: Vec<HeaderEntry>,
) -> Result<Fetcher<Vec<BlockEntry>>> {
    if let Some(tip) = new_headers.last() {
        debug!("{:?} ({} left to index)", tip, new_headers.len());
    };
    let daemon = daemon.reconnect()?;
    let batch_size = block_batch_size(daemon.network());
    let chan = SyncChannel::new(1);
    let sender = chan.sender();
    Ok(Fetcher::from(
        chan.into_receiver(),
        spawn_thread("bitcoind_fetcher", move || {
            for entries in new_headers.chunks(batch_size) {
                let blockhashes: Vec<BlockHash> = entries.iter().map(|e| *e.hash()).collect();
                let sized_blocks = daemon
                    .getblocks_with_size(&blockhashes)
                    .expect("failed to get blocks from bitcoind");
                assert_eq!(sized_blocks.len(), entries.len());
                let block_entries: Vec<BlockEntry> = sized_blocks
                    .into_iter()
                    .zip(entries)
                    .map(|((block, size), entry)| BlockEntry {
                        entry: entry.clone(), // TODO: remove this clone()
                        weight: block_weight(daemon.network(), &block, size),
                        size,
                        block,
                    })
                    .collect();
                assert_eq!(block_entries.len(), entries.len());
                sender
                    .send(block_entries)
                    .expect("failed to send fetched blocks");
            }
        }),
    ))
}

fn blkfiles_fetcher(
    daemon: &Daemon,
    new_headers: Vec<HeaderEntry>,
) -> Result<Fetcher<Vec<BlockEntry>>> {
    let magic = daemon.magic();
    let network = daemon.network();
    let blk_files = daemon.list_blk_files()?;

    let chan = SyncChannel::new(1);
    let sender = chan.sender();

    let mut entry_map: HashMap<BlockHash, HeaderEntry> =
        new_headers.into_iter().map(|h| (*h.hash(), h)).collect();

    let parser = blkfiles_parser(blkfiles_reader(blk_files), magic, network);
    Ok(Fetcher::from(
        chan.into_receiver(),
        spawn_thread("blkfiles_fetcher", move || {
            parser.map(|sizedblocks| {
                let block_entries: Vec<BlockEntry> = sizedblocks
                    .into_iter()
                    .filter_map(|(block, size)| {
                        let blockhash = block.block_hash();
                        entry_map
                            .remove(&blockhash)
                            .map(|entry| BlockEntry {
                                weight: block_weight(network, &block, size),
                                block,
                                entry,
                                size,
                            })
                            .or_else(|| {
                                trace!("skipping block {}", blockhash);
                                None
                            })
                    })
                    .collect();
                trace!("fetched {} blocks", block_entries.len());
                sender
                    .send(block_entries)
                    .expect("failed to send blocks entries from blk*.dat files");
            });
            if !entry_map.is_empty() {
                panic!(
                    "failed to index {} blocks from blk*.dat files",
                    entry_map.len()
                )
            }
        }),
    ))
}

fn blkfiles_reader(blk_files: Vec<PathBuf>) -> Fetcher<Vec<u8>> {
    let chan = SyncChannel::new(1);
    let sender = chan.sender();
    let xor_key = blk_files.first().and_then(|p| {
        let xor_file = p
            .parent()
            .expect("blk.dat files must exist in a directory")
            .join("xor.dat");
        if xor_file.exists() {
            Some(fs::read(xor_file).expect("xor.dat exists"))
        } else {
            None
        }
    });

    Fetcher::from(
        chan.into_receiver(),
        spawn_thread("blkfiles_reader", move || {
            for path in blk_files {
                trace!("reading {:?}", path);
                let mut blob = fs::read(&path)
                    .unwrap_or_else(|e| panic!("failed to read {:?}: {:?}", path, e));

                // If the xor.dat exists. Use it to decrypt the block files.
                if let Some(xor_key) = &xor_key {
                    for (&key, byte) in xor_key.iter().cycle().zip(blob.iter_mut()) {
                        *byte ^= key;
                    }
                }

                sender
                    .send(blob)
                    .unwrap_or_else(|_| panic!("failed to send {:?} contents", path));
            }
        }),
    )
}

fn blkfiles_parser(
    blobs: Fetcher<Vec<u8>>,
    magic: u32,
    network: Network,
) -> Fetcher<Vec<SizedBlock>> {
    let chan = SyncChannel::new(1);
    let sender = chan.sender();

    Fetcher::from(
        chan.into_receiver(),
        spawn_thread("blkfiles_parser", move || {
            blobs.map(|blob| {
                trace!("parsing {} bytes", blob.len());
                let blocks =
                    parse_blocks(blob, magic, network).expect("failed to parse blk*.dat file");
                sender
                    .send(blocks)
                    .expect("failed to send blocks from blk*.dat file");
            });
        }),
    )
}

fn parse_blocks(blob: Vec<u8>, magic: u32, network: Network) -> Result<Vec<SizedBlock>> {
    let mut cursor = Cursor::new(&blob);
    let mut slices = vec![];
    let max_pos = blob.len() as u64;

    while cursor.position() < max_pos {
        let offset = cursor.position();
        match u32::consensus_decode(&mut cursor) {
            Ok(value) => {
                if magic != value {
                    cursor.set_position(offset + 1);
                    continue;
                }
            }
            Err(_) => break, // EOF
        };
        let block_size = u32::consensus_decode(&mut cursor).chain_err(|| "no block size")?;
        let start = cursor.position();
        let end = start + block_size as u64;

        // If Core's WriteBlockToDisk ftell fails, only the magic bytes and size will be written
        // and the block body won't be written to the blk*.dat file.
        // Since the first 4 bytes should contain the block's version, we can skip such blocks
        // by peeking the cursor (and skipping previous `magic` and `block_size`).
        match u32::consensus_decode(&mut cursor) {
            Ok(value) => {
                if magic == value {
                    cursor.set_position(start);
                    continue;
                }
            }
            Err(_) => break, // EOF
        }
        slices.push((&blob[start as usize..end as usize], block_size));
        cursor.set_position(end);
    }

    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(0) // CPU-bound
        .thread_name(|i| format!("parse-blocks-{}", i))
        .build()
        .unwrap();
    Ok(pool.install(|| {
        slices
            .into_par_iter()
            .map(|(slice, size)| {
                (
                    qbit_or_default_block_from_bytes(network, slice, "blk*.dat block")
                        .expect("failed to parse Block"),
                    size,
                )
            })
            .collect()
    }))
}

#[cfg(all(test, not(feature = "liquid")))]
mod tests {
    use super::*;

    fn fixture_hex(path: &str) -> Vec<u8> {
        let hex = std::fs::read_to_string(path).expect("fixture hex should be readable");
        hex::decode(hex.split_whitespace().collect::<String>()).expect("fixture hex should decode")
    }

    fn blk_file_frame(network: Network, raw_block: &[u8]) -> Vec<u8> {
        let mut blob = Vec::new();
        blob.extend_from_slice(&network.magic().to_le_bytes());
        blob.extend_from_slice(&(raw_block.len() as u32).to_le_bytes());
        blob.extend_from_slice(raw_block);
        blob
    }

    #[test]
    fn qbit_block_batch_size_is_conservative() {
        assert_eq!(block_batch_size(Network::Bitcoin), DEFAULT_BLOCK_BATCH_SIZE);
        assert_eq!(block_batch_size(Network::Regtest), DEFAULT_BLOCK_BATCH_SIZE);
        assert_eq!(block_batch_size(Network::Qbit), QBIT_BLOCK_BATCH_SIZE);
        assert_eq!(
            block_batch_size(Network::QbitTestnet4),
            QBIT_BLOCK_BATCH_SIZE
        );
        assert_eq!(
            block_batch_size(Network::QbitRegtest),
            QBIT_BLOCK_BATCH_SIZE
        );
        assert!(QBIT_BLOCK_BATCH_SIZE < DEFAULT_BLOCK_BATCH_SIZE);
    }

    #[test]
    fn qbit_parse_blocks_uses_auxpow_aware_decoder() {
        let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-qbitd-auxpow-block.hex");
        let blocks = parse_blocks(
            blk_file_frame(Network::QbitRegtest, &raw),
            Network::QbitRegtest.magic(),
            Network::QbitRegtest,
        )
        .expect("qbit blk*.dat frame should parse");

        assert_eq!(blocks.len(), 1);
        let (block, size) = &blocks[0];
        assert_eq!(*size as usize, raw.len());
        assert_eq!(
            block.block_hash().to_string(),
            "fd7f94d1992a159f2ff0311d92e23fa7f880ca285d56da6765340f04d3c88aca"
        );
        assert_eq!(block.txdata.len(), 1);
        assert_eq!(
            block_weight(Network::QbitRegtest, block, *size),
            raw.len() as u32
        );
        assert_ne!(
            block_weight(Network::QbitRegtest, block, *size),
            block.weight() as u32,
            "qbit WSF=1 metadata must not use rust-bitcoin WSF=4 weight"
        );
    }

    #[test]
    fn qbit_parse_blocks_ignores_other_network_magic() {
        let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-qbitd-auxpow-block.hex");
        let blocks = parse_blocks(
            blk_file_frame(Network::QbitRegtest, &raw),
            Network::Regtest.magic(),
            Network::QbitRegtest,
        )
        .expect("wrong-magic blk*.dat frame should scan without parser failure");

        assert!(blocks.is_empty());
    }
}
