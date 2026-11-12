use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Lines, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64;
use bitcoin::hashes::hex::{FromHex, ToHex};
use glob;
use hex;
use itertools::Itertools;
use serde_json::{from_str, from_value, Value};

#[cfg(not(feature = "liquid"))]
use bitcoin::consensus::encode::{deserialize, serialize};
#[cfg(feature = "liquid")]
use elements::encode::{deserialize, serialize};

use crate::chain::{genesis_hash, Block, BlockHash, BlockHeader, Network, Transaction, Txid};
use crate::config::BITCOIND_SUBVER;
use crate::metrics::{HistogramOpts, HistogramVec, Metrics};
#[cfg(not(feature = "liquid"))]
use crate::qbit_codec;
use crate::signal::Waiter;
use crate::util::HeaderList;

use crate::errors::*;

const DEFAULT_TX_BATCH_SIZE: usize = 50_000;
// P2MR/PQC witnesses can make qbit raw transaction replies much larger than
// Bitcoin replies, so keep qbit RPC batches conservative until #20 tunes them
// against live data.
const QBIT_TX_BATCH_SIZE: usize = 128;
const MIN_SUPPORTED_BITCOIND_VERSION: u64 = 16_00_00;

fn tx_batch_size(network: Network) -> usize {
    if network.is_qbit() {
        QBIT_TX_BATCH_SIZE
    } else {
        DEFAULT_TX_BATCH_SIZE
    }
}

fn parse_hash<T>(value: &Value) -> Result<T>
where
    T: FromHex,
{
    T::from_hex(
        value
            .as_str()
            .chain_err(|| format!("non-string value: {}", value))?,
    )
    .chain_err(|| format!("non-hex value: {}", value))
}

fn header_from_value(network: Network, value: Value) -> Result<BlockHeader> {
    let header_hex = value
        .as_str()
        .chain_err(|| format!("non-string header: {}", value))?;
    let header_bytes = hex::decode(header_hex).chain_err(|| "non-hex header")?;
    qbit_or_default_header_from_bytes(network, &header_bytes, header_hex)
}

fn qbit_or_default_header_from_bytes(
    _network: Network,
    header_bytes: &[u8],
    header_hex: &str,
) -> Result<BlockHeader> {
    #[cfg(not(feature = "liquid"))]
    if _network.is_qbit() {
        if header_bytes.len() < qbit_codec::PURE_BLOCK_HEADER_LEN {
            bail!(format!(
                "failed to parse qbit header {}: got {} bytes, expected at least {}",
                header_hex,
                header_bytes.len(),
                qbit_codec::PURE_BLOCK_HEADER_LEN
            ));
        }

        let header = qbit_codec::deserialize_header(header_bytes)
            .chain_err(|| format!("failed to parse qbit header {}", header_hex))?;
        return Ok(header);
    }

    deserialize(header_bytes).chain_err(|| format!("failed to parse header {}", header_hex))
}

fn block_from_value(network: Network, value: Value) -> Result<(Block, u32)> {
    let block_hex = value.as_str().chain_err(|| "non-string block")?;
    let block_bytes = hex::decode(block_hex).chain_err(|| "non-hex block")?;
    let raw_size = block_bytes.len() as u32;
    let block = qbit_or_default_block_from_bytes(network, &block_bytes, block_hex)?;
    Ok((block, raw_size))
}

pub(crate) fn qbit_or_default_block_from_bytes(
    _network: Network,
    block_bytes: &[u8],
    block_label: &str,
) -> Result<Block> {
    #[cfg(not(feature = "liquid"))]
    if _network.is_qbit() {
        return qbit_codec::deserialize_block_as_bitcoin(block_bytes)
            .chain_err(|| format!("failed to parse qbit block {}", block_label));
    }

    deserialize(block_bytes).chain_err(|| format!("failed to parse block {}", block_label))
}

fn tx_from_value(_network: Network, value: Value) -> Result<Transaction> {
    let tx_hex = value.as_str().chain_err(|| "non-string tx")?;
    let tx_bytes = hex::decode(tx_hex).chain_err(|| "non-hex tx")?;
    #[cfg(not(feature = "liquid"))]
    if _network.is_qbit() {
        return qbit_codec::deserialize_transaction(&tx_bytes)
            .chain_err(|| format!("failed to parse qbit tx {}", tx_hex));
    }

    deserialize(&tx_bytes).chain_err(|| format!("failed to parse tx {}", tx_hex))
}

/// Parse JSONRPC error code, if exists.
fn parse_error_code(err: &Value) -> Option<i64> {
    err.as_object()?.get("code")?.as_i64()
}

fn parse_jsonrpc_reply(mut reply: Value, method: &str, expected_id: u64) -> Result<Value> {
    if let Some(reply_obj) = reply.as_object_mut() {
        if let Some(err) = reply_obj.get("error") {
            if !err.is_null() {
                if let Some(code) = parse_error_code(err) {
                    match code {
                        // RPC_IN_WARMUP -> retry by later reconnection
                        -28 => bail!(ErrorKind::Connection(err.to_string())),
                        _ => bail!("{} RPC error: {}", method, err),
                    }
                }
            }
        }
        let id = reply_obj
            .get("id")
            .chain_err(|| format!("no id in reply: {:?}", reply_obj))?
            .clone();
        if id != expected_id {
            bail!(
                "wrong {} response id {}, expected {}",
                method,
                id,
                expected_id
            );
        }
        if let Some(result) = reply_obj.get_mut("result") {
            return Ok(result.take());
        }
        bail!("no result in reply: {:?}", reply_obj);
    }
    bail!("non-object reply: {:?}", reply);
}

#[derive(Serialize, Deserialize, Debug)]
pub struct BlockchainInfo {
    pub chain: String,
    pub blocks: u32,
    pub headers: u32,
    pub bestblockhash: String,
    pub pruned: bool,
    pub verificationprogress: f32,
    pub initialblockdownload: Option<bool>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct MempoolInfo {
    pub loaded: bool,
}

#[derive(Serialize, Deserialize, Debug)]
struct NetworkInfo {
    version: u64,
    subversion: String,
    relayfee: f64, // in BTC/kB
}

fn validate_daemon_network_info(network: Network, network_info: &NetworkInfo) -> Result<()> {
    if network.is_qbit() {
        return Ok(());
    }

    if network_info.version < MIN_SUPPORTED_BITCOIND_VERSION {
        bail!(
            "{} is not supported - please use bitcoind 0.16+",
            network_info.subversion,
        )
    }

    Ok(())
}

fn validate_daemon_chain(
    network: Network,
    blockchain_info: &BlockchainInfo,
    daemon_genesis: Option<BlockHash>,
) -> Result<()> {
    let expected_chain = network.daemon_chain_name();
    if blockchain_info.chain != expected_chain {
        bail!(format!(
            "daemon chain mismatch: selected network {} expects getblockchaininfo.chain={}, got {}",
            network.canonical_name(),
            expected_chain,
            blockchain_info.chain
        ));
    }

    if network.has_static_genesis_hash() {
        let expected_genesis = genesis_hash(network);
        let daemon_genesis = daemon_genesis.chain_err(|| {
            format!(
                "missing daemon genesis hash for selected network {}",
                network.canonical_name()
            )
        })?;
        if daemon_genesis != expected_genesis {
            bail!(format!(
                "daemon genesis mismatch: selected network {} expects {}, got {}",
                network.canonical_name(),
                expected_genesis,
                daemon_genesis
            ));
        }
    }

    Ok(())
}

#[derive(Serialize, Deserialize, Debug)]
struct MempoolFees {
    base: f64,
    #[serde(rename = "effective-feerate")]
    effective_feerate: f64,
    #[serde(rename = "effective-includes")]
    effective_includes: Vec<String>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct MempoolAcceptResult {
    txid: String,
    wtxid: String,
    allowed: Option<bool>,
    vsize: Option<u32>,
    fees: Option<MempoolFees>,
    #[serde(rename = "reject-reason")]
    reject_reason: Option<String>,
}

#[derive(Serialize, Deserialize, Debug)]
struct MempoolFeesSubmitPackage {
    base: f64,
    #[serde(rename = "effective-feerate")]
    effective_feerate: Option<f64>,
    #[serde(rename = "effective-includes")]
    effective_includes: Option<Vec<String>>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct SubmitPackageResult {
    package_msg: String,
    #[serde(rename = "tx-results")]
    tx_results: HashMap<String, TxResult>,
    #[serde(rename = "replaced-transactions")]
    replaced_transactions: Option<Vec<String>>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct TxResult {
    txid: String,
    #[serde(rename = "other-wtxid")]
    other_wtxid: Option<String>,
    vsize: Option<u32>,
    fees: Option<MempoolFeesSubmitPackage>,
    error: Option<String>,
}

pub trait CookieGetter: Send + Sync {
    fn get(&self) -> Result<Vec<u8>>;
}

struct Connection {
    tx: TcpStream,
    rx: Lines<BufReader<TcpStream>>,
    cookie_getter: Arc<dyn CookieGetter>,
    addr: SocketAddr,
    signal: Waiter,
}

fn tcp_connect(addr: SocketAddr, signal: &Waiter) -> Result<TcpStream> {
    loop {
        match TcpStream::connect(addr) {
            Ok(conn) => return Ok(conn),
            Err(err) => {
                warn!("failed to connect daemon at {}: {}", addr, err);
                signal.wait(Duration::from_secs(3), false)?;
                continue;
            }
        }
    }
}

impl Connection {
    fn new(
        addr: SocketAddr,
        cookie_getter: Arc<dyn CookieGetter>,
        signal: Waiter,
    ) -> Result<Connection> {
        let conn = tcp_connect(addr, &signal)?;
        let reader = BufReader::new(
            conn.try_clone()
                .chain_err(|| format!("failed to clone {:?}", conn))?,
        );
        Ok(Connection {
            tx: conn,
            rx: reader.lines(),
            cookie_getter,
            addr,
            signal,
        })
    }

    fn reconnect(&self) -> Result<Connection> {
        Connection::new(self.addr, self.cookie_getter.clone(), self.signal.clone())
    }

    fn send(&mut self, request: &str) -> Result<()> {
        let cookie = &self.cookie_getter.get()?;
        let msg = format!(
            "POST / HTTP/1.1\nAuthorization: Basic {}\nContent-Length: {}\n\n{}",
            base64::encode(cookie),
            request.len(),
            request,
        );
        self.tx.write_all(msg.as_bytes()).chain_err(|| {
            ErrorKind::Connection("disconnected from daemon while sending".to_owned())
        })
    }

    fn recv(&mut self) -> Result<String> {
        // TODO: use proper HTTP parser.
        let mut in_header = true;
        let mut contents: Option<String> = None;
        let iter = self.rx.by_ref();
        let status = iter
            .next()
            .chain_err(|| {
                ErrorKind::Connection("disconnected from daemon while receiving".to_owned())
            })?
            .chain_err(|| ErrorKind::Connection("failed to read status".to_owned()))?;
        let mut headers = HashMap::new();
        for line in iter {
            let line = line.chain_err(|| ErrorKind::Connection("failed to read".to_owned()))?;
            if line.is_empty() {
                in_header = false; // next line should contain the actual response.
            } else if in_header {
                let parts: Vec<&str> = line.splitn(2, ": ").collect();
                if parts.len() == 2 {
                    headers.insert(parts[0].to_owned(), parts[1].to_owned());
                } else {
                    warn!("invalid header: {:?}", line);
                }
            } else {
                contents = Some(line);
                break;
            }
        }

        let contents =
            contents.chain_err(|| ErrorKind::Connection("no reply from daemon".to_owned()))?;
        let contents_length: &str = headers
            .get("Content-Length")
            .chain_err(|| format!("Content-Length is missing: {:?}", headers))?;
        let contents_length: usize = contents_length
            .parse()
            .chain_err(|| format!("invalid Content-Length: {:?}", contents_length))?;

        let expected_length = contents_length - 1; // trailing EOL is skipped
        if expected_length != contents.len() {
            bail!(ErrorKind::Connection(format!(
                "expected {} bytes, got {}",
                expected_length,
                contents.len()
            )));
        }

        Ok(if status == "HTTP/1.1 200 OK" {
            contents
        } else if status == "HTTP/1.1 500 Internal Server Error" {
            warn!("HTTP status: {}", status);
            contents // the contents should have a JSONRPC error field
        } else {
            bail!(
                "request failed {:?}: {:?} = {:?}",
                status,
                headers,
                contents
            );
        })
    }
}

struct Counter {
    value: Mutex<u64>,
}

impl Counter {
    fn new() -> Self {
        Counter {
            value: Mutex::new(0),
        }
    }

    fn next(&self) -> u64 {
        let mut value = self.value.lock().unwrap();
        *value += 1;
        *value
    }
}

pub struct Daemon {
    daemon_dir: PathBuf,
    blocks_dir: PathBuf,
    network: Network,
    magic: Option<u32>,
    conn: Mutex<Connection>,
    message_id: Counter, // for monotonic JSONRPC 'id'
    signal: Waiter,

    // monitoring
    latency: HistogramVec,
    size: HistogramVec,
}

impl Daemon {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        daemon_dir: PathBuf,
        blocks_dir: PathBuf,
        daemon_rpc_addr: SocketAddr,
        cookie_getter: Arc<dyn CookieGetter>,
        network: Network,
        magic: Option<u32>,
        signal: Waiter,
        metrics: &Metrics,
    ) -> Result<Daemon> {
        let daemon = Daemon {
            daemon_dir,
            blocks_dir,
            network,
            magic,
            conn: Mutex::new(Connection::new(
                daemon_rpc_addr,
                cookie_getter,
                signal.clone(),
            )?),
            message_id: Counter::new(),
            signal: signal.clone(),
            latency: metrics.histogram_vec(
                HistogramOpts::new("daemon_rpc", "Bitcoind RPC latency (in seconds)"),
                &["method"],
            ),
            size: metrics.histogram_vec(
                HistogramOpts::new("daemon_bytes", "Bitcoind RPC size (in bytes)"),
                &["method", "dir"],
            ),
        };
        let network_info = daemon.getnetworkinfo()?;
        info!("{:?}", network_info);
        validate_daemon_network_info(network, &network_info)?;
        // Insert the subversion (/Satoshi xx.xx.xx(comment)/) string from bitcoind
        _ = BITCOIND_SUBVER.set(network_info.subversion);

        let blockchain_info = daemon.getblockchaininfo()?;
        info!("{:?}", blockchain_info);
        let daemon_genesis = if network.has_static_genesis_hash() {
            Some(daemon.getblockhash(0)?)
        } else {
            None
        };
        validate_daemon_chain(network, &blockchain_info, daemon_genesis)?;
        if blockchain_info.pruned {
            bail!("pruned node is not supported (use '-prune=0' bitcoind flag)".to_owned())
        }
        loop {
            let info = daemon.getblockchaininfo()?;
            let mempool = daemon.getmempoolinfo()?;

            let ibd_done = if network.is_regtest() {
                info.blocks == info.headers
            } else {
                !info.initialblockdownload.unwrap_or(false)
            };

            if mempool.loaded && ibd_done && info.blocks == info.headers {
                break;
            }

            warn!(
                "waiting for bitcoind sync and mempool load to finish: {}/{} blocks, verification progress: {:.3}%, mempool loaded: {}",
                info.blocks,
                info.headers,
                info.verificationprogress * 100.0,
                mempool.loaded
            );
            signal.wait(Duration::from_secs(5), false)?;
        }
        Ok(daemon)
    }

    pub fn reconnect(&self) -> Result<Daemon> {
        Ok(Daemon {
            daemon_dir: self.daemon_dir.clone(),
            blocks_dir: self.blocks_dir.clone(),
            network: self.network,
            magic: self.magic,
            conn: Mutex::new(self.conn.lock().unwrap().reconnect()?),
            message_id: Counter::new(),
            signal: self.signal.clone(),
            latency: self.latency.clone(),
            size: self.size.clone(),
        })
    }

    pub fn list_blk_files(&self) -> Result<Vec<PathBuf>> {
        let path = self.blocks_dir.join("blk*.dat");
        debug!("listing block files at {:?}", path);
        let mut paths: Vec<PathBuf> = glob::glob(path.to_str().unwrap())
            .chain_err(|| "failed to list blk*.dat files")?
            .map(|res| res.unwrap())
            .collect();
        paths.sort();
        Ok(paths)
    }

    pub fn magic(&self) -> u32 {
        self.magic.unwrap_or_else(|| self.network.magic())
    }

    pub fn network(&self) -> Network {
        self.network
    }

    fn call_jsonrpc(&self, method: &str, request: &Value) -> Result<Value> {
        let mut conn = self.conn.lock().unwrap();
        let timer = self.latency.with_label_values(&[method]).start_timer();
        let request = request.to_string();
        conn.send(&request)?;
        self.size
            .with_label_values(&[method, "send"])
            .observe(request.len() as f64);
        let response = conn.recv()?;
        let result: Value = from_str(&response).chain_err(|| "invalid JSON")?;
        timer.observe_duration();
        self.size
            .with_label_values(&[method, "recv"])
            .observe(response.len() as f64);
        Ok(result)
    }

    fn handle_request_batch(
        &self,
        method: &str,
        params_list: &[Value],
        failure_threshold: f64,
    ) -> Result<Vec<Value>> {
        let id = self.message_id.next();
        let chunks = params_list
            .iter()
            .map(|params| json!({"method": method, "params": params, "id": id}))
            .chunks(50_000); // Max Amount of batched requests
        let mut results = vec![];
        let total_requests = params_list.len();
        let mut failed_requests: u64 = 0;
        let threshold = (failure_threshold * total_requests as f64).round() as u64;
        let mut n = 0;

        for chunk in &chunks {
            let reqs = chunk.collect();
            let mut replies = self.call_jsonrpc(method, &reqs)?;
            if let Some(replies_vec) = replies.as_array_mut() {
                for reply in replies_vec {
                    n += 1;
                    match parse_jsonrpc_reply(reply.take(), method, id) {
                        Ok(parsed_reply) => results.push(parsed_reply),
                        Err(e) => {
                            failed_requests += 1;
                            warn!(
                                "batch request {} {}/{} failed: {}",
                                method,
                                n,
                                total_requests,
                                e.to_string()
                            );
                            // abort and return the last error once a threshold number of requests have failed
                            if failed_requests > threshold {
                                return Err(e);
                            }
                        }
                    }
                }
            } else {
                bail!("non-array replies: {:?}", replies);
            }
        }

        Ok(results)
    }

    fn retry_request_batch(
        &self,
        method: &str,
        params_list: &[Value],
        failure_threshold: f64,
    ) -> Result<Vec<Value>> {
        loop {
            match self.handle_request_batch(method, params_list, failure_threshold) {
                Err(Error(ErrorKind::Connection(msg), _)) => {
                    warn!("reconnecting to bitcoind: {}", msg);
                    self.signal.wait(Duration::from_secs(3), false)?;
                    let mut conn = self.conn.lock().unwrap();
                    *conn = conn.reconnect()?;
                    continue;
                }
                result => return result,
            }
        }
    }

    fn request(&self, method: &str, params: Value) -> Result<Value> {
        let mut values = self.retry_request_batch(method, &[params], 0.0)?;
        assert_eq!(values.len(), 1);
        Ok(values.remove(0))
    }

    fn requests(&self, method: &str, params_list: &[Value]) -> Result<Vec<Value>> {
        self.retry_request_batch(method, params_list, 0.0)
    }

    // bitcoind JSONRPC API:

    pub fn getblockchaininfo(&self) -> Result<BlockchainInfo> {
        let info: Value = self.request("getblockchaininfo", json!([]))?;
        from_value(info).chain_err(|| "invalid blockchain info")
    }

    pub fn getblockhash(&self, height: u32) -> Result<BlockHash> {
        parse_hash(&self.request("getblockhash", json!([height]))?)
    }

    fn getmempoolinfo(&self) -> Result<MempoolInfo> {
        let info: Value = self.request("getmempoolinfo", json!([]))?;
        from_value(info).chain_err(|| "invalid mempool info")
    }

    fn getnetworkinfo(&self) -> Result<NetworkInfo> {
        let info: Value = self.request("getnetworkinfo", json!([]))?;
        from_value(info).chain_err(|| "invalid network info")
    }

    pub fn getbestblockhash(&self) -> Result<BlockHash> {
        parse_hash(&self.request("getbestblockhash", json!([]))?)
    }

    pub fn getblockheader(&self, blockhash: &BlockHash) -> Result<BlockHeader> {
        header_from_value(
            self.network,
            self.request(
                "getblockheader",
                json!([blockhash.to_hex(), /*verbose=*/ false]),
            )?,
        )
    }

    pub fn getblockheaders(&self, heights: &[usize]) -> Result<Vec<BlockHeader>> {
        let heights: Vec<Value> = heights.iter().map(|height| json!([height])).collect();
        let params_list: Vec<Value> = self
            .requests("getblockhash", &heights)?
            .into_iter()
            .map(|hash| json!([hash, /*verbose=*/ false]))
            .collect();
        let mut result = vec![];
        for h in self.requests("getblockheader", &params_list)? {
            result.push(header_from_value(self.network, h)?);
        }
        Ok(result)
    }

    pub fn getblock(&self, blockhash: &BlockHash) -> Result<Block> {
        let (block, _) = block_from_value(
            self.network,
            self.request("getblock", json!([blockhash.to_hex(), /*verbose=*/ false]))?,
        )?;
        assert_eq!(block.block_hash(), *blockhash);
        Ok(block)
    }

    pub fn getblock_raw(&self, blockhash: &BlockHash, verbose: u32) -> Result<Value> {
        self.request("getblock", json!([blockhash.to_hex(), verbose]))
    }

    pub fn getblocks(&self, blockhashes: &[BlockHash]) -> Result<Vec<Block>> {
        Ok(self
            .getblocks_with_size(blockhashes)?
            .into_iter()
            .map(|(block, _)| block)
            .collect())
    }

    pub fn getblocks_with_size(&self, blockhashes: &[BlockHash]) -> Result<Vec<(Block, u32)>> {
        let params_list: Vec<Value> = blockhashes
            .iter()
            .map(|hash| json!([hash.to_hex(), /*verbose=*/ false]))
            .collect();
        let values = self.requests("getblock", &params_list)?;
        let mut blocks = vec![];
        for (value, expected_hash) in values.into_iter().zip(blockhashes) {
            let (block, size) = block_from_value(self.network, value)?;
            assert_eq!(block.block_hash(), *expected_hash);
            blocks.push((block, size));
        }
        Ok(blocks)
    }

    pub fn gettransactions(&self, txhashes: &[&Txid]) -> Result<Vec<Transaction>> {
        let params_list: Vec<Value> = txhashes
            .iter()
            .map(|txhash| json!([txhash.to_hex(), /*verbose=*/ false]))
            .collect();
        let mut txs = vec![];
        for chunk in params_list.chunks(tx_batch_size(self.network)) {
            let values = self.retry_request_batch("getrawtransaction", chunk, 0.25)?;
            for value in values {
                txs.push(tx_from_value(self.network, value)?);
            }
        }
        // missing transactions are skipped, so the number of txs returned may be less than the number of txids requested
        Ok(txs)
    }

    pub fn gettransaction_raw(
        &self,
        txid: &Txid,
        blockhash: &BlockHash,
        verbose: bool,
    ) -> Result<Value> {
        self.request(
            "getrawtransaction",
            json!([txid.to_hex(), verbose, blockhash]),
        )
    }

    pub fn getmempooltx(&self, txhash: &Txid) -> Result<Transaction> {
        let value = self.request(
            "getrawtransaction",
            json!([txhash.to_hex(), /*verbose=*/ false]),
        )?;
        tx_from_value(self.network, value)
    }

    pub fn getmempooltxids(&self) -> Result<HashSet<Txid>> {
        let res = self.request("getrawmempool", json!([/*verbose=*/ false]))?;
        serde_json::from_value(res).chain_err(|| "invalid getrawmempool reply")
    }

    pub fn broadcast(&self, tx: &Transaction) -> Result<Txid> {
        self.broadcast_raw(&hex::encode(serialize(tx)))
    }

    pub fn broadcast_raw(&self, txhex: &str) -> Result<Txid> {
        let txid = self.request("sendrawtransaction", json!([txhex]))?;
        Txid::from_hex(txid.as_str().chain_err(|| "non-string txid")?)
            .chain_err(|| "failed to parse txid")
    }

    pub fn test_mempool_accept(
        &self,
        txhex: Vec<String>,
        maxfeerate: Option<f64>,
    ) -> Result<Vec<MempoolAcceptResult>> {
        let params = match maxfeerate {
            Some(rate) => json!([txhex, format!("{:.8}", rate)]),
            None => json!([txhex]),
        };
        let result = self.request("testmempoolaccept", params)?;
        serde_json::from_value::<Vec<MempoolAcceptResult>>(result)
            .chain_err(|| "invalid testmempoolaccept reply")
    }

    pub fn submit_package(
        &self,
        txhex: Vec<String>,
        maxfeerate: Option<f64>,
        maxburnamount: Option<f64>,
    ) -> Result<SubmitPackageResult> {
        let params = match (maxfeerate, maxburnamount) {
            (Some(rate), Some(burn)) => {
                json!([txhex, format!("{:.8}", rate), format!("{:.8}", burn)])
            }
            (Some(rate), None) => json!([txhex, format!("{:.8}", rate)]),
            (None, Some(burn)) => json!([txhex, null, format!("{:.8}", burn)]),
            (None, None) => json!([txhex]),
        };
        let result = self.request("submitpackage", params)?;
        serde_json::from_value::<SubmitPackageResult>(result)
            .chain_err(|| "invalid submitpackage reply")
    }

    // Get estimated feerates for the provided confirmation targets using a batch RPC request
    // Missing estimates are logged but do not cause a failure, whatever is available is returned
    #[allow(clippy::float_cmp)]
    pub fn estimatesmartfee_batch(&self, conf_targets: &[u16]) -> Result<HashMap<u16, f64>> {
        let params_list: Vec<Value> = conf_targets.iter().map(|t| json!([t])).collect();

        Ok(self
            .requests("estimatesmartfee", &params_list)?
            .iter()
            .zip(conf_targets)
            .filter_map(|(reply, target)| {
                if !reply["errors"].is_null() {
                    warn!(
                        "failed estimating fee for target {}: {:?}",
                        target, reply["errors"]
                    );
                    return None;
                }

                let feerate = reply["feerate"]
                    .as_f64()
                    .unwrap_or_else(|| panic!("invalid estimatesmartfee response: {:?}", reply));

                if feerate == -1f64 {
                    warn!("not enough data to estimate fee for target {}", target);
                    return None;
                }

                // from BTC/kB to sat/b
                Some((*target, feerate * 100_000f64))
            })
            .collect())
    }

    fn get_all_headers(&self, tip: &BlockHash) -> Result<Vec<BlockHeader>> {
        let info: Value = self.request("getblockheader", json!([tip.to_hex()]))?;
        let tip_height = info
            .get("height")
            .expect("missing height")
            .as_u64()
            .expect("non-numeric height") as usize;
        let all_heights: Vec<usize> = (0..=tip_height).collect();
        let chunk_size = 100_000;
        let mut result = vec![];
        for heights in all_heights.chunks(chunk_size) {
            trace!("downloading {} block headers", heights.len());
            let mut headers = self.getblockheaders(heights)?;
            assert!(headers.len() == heights.len());
            result.append(&mut headers);
        }

        let mut blockhash = BlockHash::default();
        for header in &result {
            assert_eq!(header.prev_blockhash, blockhash);
            blockhash = header.block_hash();
        }
        assert_eq!(blockhash, *tip);
        Ok(result)
    }

    // Returns a list of BlockHeaders in ascending height (i.e. the tip is last).
    pub fn get_new_headers(
        &self,
        indexed_headers: &HeaderList,
        bestblockhash: &BlockHash,
    ) -> Result<Vec<BlockHeader>> {
        // Iterate back over headers until known blockash is found:
        if indexed_headers.is_empty() {
            debug!("downloading all block headers up to {}", bestblockhash);
            return self.get_all_headers(bestblockhash);
        }
        debug!(
            "downloading new block headers ({} already indexed) from {}",
            indexed_headers.len(),
            bestblockhash,
        );
        let mut new_headers = vec![];
        let null_hash = BlockHash::default();
        let mut blockhash = *bestblockhash;
        while blockhash != null_hash {
            if indexed_headers.header_by_blockhash(&blockhash).is_some() {
                break;
            }
            let header = self
                .getblockheader(&blockhash)
                .chain_err(|| format!("failed to get {} header", blockhash))?;
            blockhash = header.prev_blockhash;
            new_headers.push(header);
        }
        trace!("downloaded {} block headers", new_headers.len());
        new_headers.reverse(); // so the tip is the last vector entry
        Ok(new_headers)
    }

    pub fn get_relayfee(&self) -> Result<f64> {
        let relayfee = self.getnetworkinfo()?.relayfee;

        // from BTC/kB to sat/b
        Ok(relayfee * 100_000f64)
    }
}

#[cfg(all(test, not(feature = "liquid")))]
mod tests {
    use super::*;
    use bitcoin::consensus::encode::serialize;

    fn fixture_hex(path: &str) -> Vec<u8> {
        let hex = std::fs::read_to_string(path).expect("fixture hex should be readable");
        hex::decode(hex.split_whitespace().collect::<String>()).expect("fixture hex should decode")
    }

    fn blockchain_info(chain: &str) -> BlockchainInfo {
        BlockchainInfo {
            chain: chain.to_string(),
            blocks: 0,
            headers: 0,
            bestblockhash: String::new(),
            pruned: false,
            verificationprogress: 1.0,
            initialblockdownload: Some(false),
        }
    }

    fn network_info(version: u64, subversion: &str) -> NetworkInfo {
        NetworkInfo {
            version,
            subversion: subversion.to_string(),
            relayfee: 0.0,
        }
    }

    #[test]
    fn bitcoin_startup_validation_rejects_unsupported_daemon_version() {
        let err = validate_daemon_network_info(
            Network::Regtest,
            &network_info(MIN_SUPPORTED_BITCOIND_VERSION - 1, "/Satoshi:0.15.2/"),
        )
        .expect_err("Bitcoin regtest must keep the inherited bitcoind 0.16+ floor");

        assert!(err.to_string().contains("bitcoind 0.16+"));
        assert!(err.to_string().contains("/Satoshi:0.15.2/"));
    }

    #[test]
    fn qbit_startup_validation_accepts_qbitd_subversion_numbering() {
        validate_daemon_network_info(
            Network::QbitRegtest,
            &network_info(100, "/qbit:0.1.0-testnet4-rc3/"),
        )
        .expect("qbitd uses qbit version numbering, not Bitcoin Core version numbers");
    }

    #[test]
    fn qbit_startup_validation_rejects_bitcoin_regtest_genesis() {
        let err = validate_daemon_chain(
            Network::QbitRegtest,
            &blockchain_info("regtest"),
            Some(genesis_hash(Network::Regtest)),
        )
        .expect_err("Bitcoin regtest genesis must not satisfy qbit regtest");

        assert!(err.to_string().contains("daemon genesis mismatch"));
        assert!(err.to_string().contains("qbitregtest"));
    }

    #[test]
    fn qbit_startup_validation_rejects_wrong_chain_label() {
        let err = validate_daemon_chain(
            Network::QbitTestnet4,
            &blockchain_info("test"),
            Some(genesis_hash(Network::QbitTestnet4)),
        )
        .expect_err("qbit testnet4 must require the daemon testnet4 chain");

        assert!(err.to_string().contains("daemon chain mismatch"));
        assert!(err.to_string().contains("testnet4"));
    }

    #[test]
    fn qbit_startup_validation_accepts_matching_chain_and_genesis() {
        validate_daemon_chain(
            Network::QbitTestnet4,
            &blockchain_info("testnet4"),
            Some(genesis_hash(Network::QbitTestnet4)),
        )
        .expect("matching qbit testnet4 daemon facts should pass");
    }

    #[test]
    fn qbit_tx_batch_size_is_conservative() {
        assert_eq!(tx_batch_size(Network::Bitcoin), DEFAULT_TX_BATCH_SIZE);
        assert_eq!(tx_batch_size(Network::Regtest), DEFAULT_TX_BATCH_SIZE);
        assert_eq!(tx_batch_size(Network::Qbit), QBIT_TX_BATCH_SIZE);
        assert_eq!(tx_batch_size(Network::QbitTestnet4), QBIT_TX_BATCH_SIZE);
        assert_eq!(tx_batch_size(Network::QbitRegtest), QBIT_TX_BATCH_SIZE);
        assert!(QBIT_TX_BATCH_SIZE < DEFAULT_TX_BATCH_SIZE);
    }

    #[test]
    fn qbit_block_from_rpc_value_uses_auxpow_aware_parser_and_raw_size() {
        let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-qbitd-auxpow-block.hex");
        let (block, raw_size) =
            block_from_value(Network::QbitRegtest, Value::String(hex::encode(&raw)))
                .expect("qbit AuxPoW block should parse through daemon helper");

        assert_eq!(raw_size as usize, raw.len());
        assert_eq!(
            block.block_hash().to_string(),
            "fd7f94d1992a159f2ff0311d92e23fa7f880ca285d56da6765340f04d3c88aca"
        );
        assert_eq!(block.txdata.len(), 1);
        assert!(
            raw_size as usize > serialize(&block).len(),
            "raw qbit block size must preserve AuxPoW bytes that bitcoin::Block cannot store"
        );
    }

    #[test]
    fn qbit_tx_from_rpc_value_preserves_witness_bytes() {
        let raw = fixture_hex("tests/fixtures/qbit/transactions/regtest-witness-tx.hex");
        let tx = tx_from_value(Network::QbitRegtest, Value::String(hex::encode(&raw)))
            .expect("qbit witness transaction should parse through daemon helper");

        assert_eq!(serialize(&tx), raw);
        assert_eq!(tx.input.len(), 1);
        assert_eq!(tx.input[0].witness.len(), 1);
        assert_eq!(tx.input[0].witness.iter().next().unwrap(), &[0xab, 0xcd]);
    }

    #[test]
    fn qbit_header_from_rpc_value_accepts_extended_auxpow_header_payload() {
        let header_payload =
            fixture_hex("tests/fixtures/qbit/headers/regtest-qbitd-auxpow-extended-header.hex");
        let header = header_from_value(
            Network::QbitRegtest,
            Value::String(hex::encode(&header_payload)),
        )
        .expect("qbit AuxPoW header payload should normalize to pure header");

        assert!(qbit_codec::has_auxpow_flag(header.version));
        assert_eq!(
            header.block_hash().to_string(),
            "fd7f94d1992a159f2ff0311d92e23fa7f880ca285d56da6765340f04d3c88aca"
        );
    }

    #[test]
    fn qbit_header_from_rpc_value_rejects_auxpow_header_with_trailing_tx_vector() {
        let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-qbitd-auxpow-block.hex");
        let err = header_from_value(Network::QbitRegtest, Value::String(hex::encode(&raw)))
            .expect_err("AuxPoW header parser must reject whole-block trailing transaction bytes");

        assert!(err.to_string().contains("failed to parse qbit header"));
    }

    #[test]
    fn qbit_header_from_rpc_value_rejects_non_auxpow_trailing_bytes() {
        let raw = fixture_hex("tests/fixtures/qbit/blocks/regtest-synthetic-non-auxpow-block.hex");
        let err = header_from_value(Network::QbitRegtest, Value::String(hex::encode(&raw)))
            .expect_err("non-AuxPoW qbit header must not accept trailing block bytes");

        assert!(err.to_string().contains("failed to parse qbit header"));
    }
}
