#!/usr/bin/env python3
"""Run a read-only live qbit testnet4 canary against qbit-electrs.

The canary connects to an existing qbit testnet4 RPC endpoint, starts a local
qbit-electrs process with a temporary DB, waits for tip parity, and compares
selected REST/Electrum responses to qbit RPC truth. It never calls mutating
qbit RPC methods.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "doc" / "qbit-contract.md"
FIXTURE_MANIFEST = ROOT / "tests" / "fixtures" / "qbit" / "manifest.json"
TESTNET4_GENESIS = "000000000000796fe86bbc0bf1b66a07e4b4c0676f74b54cf7e5ce8b3f1a0090"
PURE_BLOCK_HEADER_LEN = 80
PURE_BLOCK_HEADER_HEX_LEN = PURE_BLOCK_HEADER_LEN * 2
DEFAULT_STRESS_HEIGHT = 17_000
AUXPOW_VERSION_FLAG = 0x0000_0100
BIP9_TOP_BITS = 0x2000_0000
DEFAULT_MEMPOOL_TXID_COMPARE_LIMIT = 10_000
BTC_PER_KB_TO_SAT_PER_VB = 100_000.0
FEE_ESTIMATE_TARGETS = list(range(1, 26)) + [144, 504, 1008]
FEE_ESTIMATE_BTC_KB_TOLERANCE = 1e-12
FEE_ESTIMATE_SAT_VB_TOLERANCE = 1e-8
SELECTED_METRICS = {
    "electrum_clients",
    "mempool_count",
    "process_cpu_usage",
    "process_fs_fds",
    "process_memory_rss",
    "tip_height",
}


class CanaryError(Exception):
    pass


def info(message: str) -> None:
    print(f"[qbit-canary] {message}", flush=True)


def display_arg(part: object) -> str:
    text = str(part)
    if "cookie" in text.lower() or len(text) > 180:
        return "<redacted>"
    return text


def shlex_join(command: list[object]) -> str:
    return shlex.join(display_arg(part) for part in command)


def run_capture(command: list[object], check: bool = True) -> str:
    info(f"+ {shlex_join(command)}")
    proc = subprocess.run(
        [str(part) for part in command],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise CanaryError(
            "command failed with exit code "
            f"{proc.returncode}: {shlex_join(command)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout.strip()


def read_contract_commit() -> str:
    text = CONTRACT_PATH.read_text()
    match = re.search(
        r"qbit source ref: `(?:origin/main|pinned snapshot)` at `([0-9a-f]{40})`",
        text,
    )
    if not match:
        raise CanaryError(f"could not find qbit source ref in {CONTRACT_PATH}")
    return match.group(1)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def tail_file(path: Path, max_bytes: int = 20_000) -> str:
    if not path.exists():
        return f"{path} does not exist"
    data = path.read_bytes()
    return data[-max_bytes:].decode(errors="replace")


def http_text(port: int, path: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return response.read().decode()


def http_json(port: int, path: str, timeout: float = 10.0):
    return json.loads(http_text(port, path, timeout))


def block_hash_from_header_hex(header_hex: str) -> str:
    header = bytes.fromhex(header_hex)
    if len(header) != PURE_BLOCK_HEADER_LEN:
        raise CanaryError(f"expected 80-byte header, got {len(header)} bytes")
    first = hashlib.sha256(header).digest()
    return hashlib.sha256(first).digest()[::-1].hex()


def split_qbit_rpc_header_hex(header_hex: object, label: str) -> tuple[str, str]:
    if not isinstance(header_hex, str):
        raise CanaryError(f"qbit getblockheader returned non-string {label}: {header_hex}")
    try:
        header_bytes = bytes.fromhex(header_hex)
    except ValueError as exc:
        raise CanaryError(f"qbit getblockheader returned non-hex {label}: {header_hex}") from exc
    if len(header_bytes) < PURE_BLOCK_HEADER_LEN:
        raise CanaryError(
            f"qbit getblockheader returned short {label}: "
            f"{len(header_bytes)} bytes, expected at least {PURE_BLOCK_HEADER_LEN}"
        )
    return (
        header_hex[:PURE_BLOCK_HEADER_HEX_LEN].lower(),
        header_hex[PURE_BLOCK_HEADER_HEX_LEN:].lower(),
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).digest().hex()


def normalize_rpc_addr(value: str) -> tuple[str, str]:
    if "://" not in value:
        return value, f"http://{value}"
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or not parsed.netloc:
        raise CanaryError("--daemon-rpc-addr must be host:port or http://host:port")
    return parsed.netloc, value.rstrip("/")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "qbit-node"


def load_cookie(args: argparse.Namespace) -> tuple[str, str]:
    if args.cookie:
        return args.cookie, "--cookie"
    cookie_file = args.cookie_file or os.environ.get("QBIT_RPC_COOKIE_FILE")
    if cookie_file:
        return Path(cookie_file).read_text().strip(), "cookie file"
    if os.environ.get("QBIT_RPC_COOKIE"):
        return os.environ["QBIT_RPC_COOKIE"], "QBIT_RPC_COOKIE"
    raise CanaryError("provide qbit RPC auth via --cookie-file, QBIT_RPC_COOKIE_FILE, or QBIT_RPC_COOKIE")


def has_auxpow_flag(version: int) -> bool:
    return (version & BIP9_TOP_BITS) == BIP9_TOP_BITS and (version & AUXPOW_VERSION_FLAG) != 0


def metric_name(line: str) -> str:
    return line.split("{", 1)[0].split(None, 1)[0]


def selected_metric_samples(text: str) -> list[dict[str, str]]:
    samples = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = metric_name(line)
        if name not in SELECTED_METRICS:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        samples.append(
            {
                "name": name,
                "series": parts[0],
                "value": parts[1],
            }
        )
    return samples


def finite_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CanaryError(f"{label} is not numeric: {value}")
    number = float(value)
    if not math.isfinite(number):
        raise CanaryError(f"{label} is not finite: {value}")
    return number


def electrum_request(file, request_id: int, method: str, params: list[object]):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    file.write(json.dumps(request).encode() + b"\n")
    file.flush()
    line = file.readline()
    if not line:
        raise CanaryError(f"Electrum connection closed waiting for {method}")
    response = json.loads(line.decode())
    if response.get("id") != request_id:
        raise CanaryError(f"unexpected Electrum response id: {response}")
    if response.get("error"):
        raise CanaryError(f"Electrum {method} failed: {response['error']}")
    return response.get("result")


class QbitRpc:
    def __init__(self, rpc_url: str, cookie: str) -> None:
        self.rpc_url = rpc_url
        token = base64.b64encode(cookie.encode()).decode()
        self.auth_header = f"Basic {token}"
        self.request_id = 0

    def call(self, method: str, params: list[object] | None = None):
        self.request_id += 1
        payload = json.dumps(
            {
                "jsonrpc": "1.0",
                "id": f"qbit-canary-{self.request_id}",
                "method": method,
                "params": params or [],
            }
        ).encode()
        request = urllib.request.Request(
            self.rpc_url,
            data=payload,
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise CanaryError(f"qbit RPC {method} failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise CanaryError(f"qbit RPC {method} connection failed: {exc}") from exc
        parsed = json.loads(body)
        if parsed.get("error") is not None:
            raise CanaryError(f"qbit RPC {method} returned error: {parsed['error']}")
        return parsed.get("result")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a read-only qbit testnet4 canary against qbit-electrs.",
    )
    parser.add_argument(
        "--node",
        default=os.environ.get("QBIT_CANARY_NODE"),
        help="human-readable node label for artifact paths, for example fermion-testnet4",
    )
    parser.add_argument(
        "--daemon-rpc-addr",
        "--qbit-rpc",
        dest="daemon_rpc_addr",
        default=os.environ.get("QBIT_DAEMON_RPC_ADDR", "127.0.0.1:48352"),
        help="qbit testnet4 RPC host:port, or http://host:port",
    )
    parser.add_argument(
        "--cookie",
        default=None,
        help="qbit RPC auth cookie USER:PASSWORD. Prefer QBIT_RPC_COOKIE or --cookie-file.",
    )
    parser.add_argument(
        "--cookie-file",
        default=None,
        help="file containing qbit RPC auth cookie USER:PASSWORD",
    )
    parser.add_argument("--electrs-bin", default=os.environ.get("ELECTRS_BIN"))
    parser.add_argument("--build-electrs", action="store_true")
    parser.add_argument(
        "--sync-timeout",
        type=int,
        default=int(os.environ.get("QBIT_CANARY_SYNC_TIMEOUT", "1800")),
        help="seconds to wait for initial electrs tip parity",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=int(os.environ.get("QBIT_CANARY_DURATION", "120")),
        help="seconds to keep checking live tip parity after initial sync",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("QBIT_CANARY_INTERVAL", "60")),
        help="seconds between live parity checks",
    )
    parser.add_argument(
        "--mempool-sync-timeout",
        type=int,
        default=int(os.environ.get("QBIT_CANARY_MEMPOOL_SYNC_TIMEOUT", "60")),
        help="seconds to wait for electrs REST mempool parity with qbit RPC",
    )
    parser.add_argument(
        "--mempool-txid-compare-limit",
        type=int,
        default=int(
            os.environ.get(
                "QBIT_CANARY_MEMPOOL_TXID_COMPARE_LIMIT",
                str(DEFAULT_MEMPOOL_TXID_COMPARE_LIMIT),
            )
        ),
        help="maximum mempool txids to compare as a full set before recording counts only",
    )
    parser.add_argument(
        "--allow-missing-fee-estimates",
        action="store_true",
        default=os.environ.get("QBIT_CANARY_ALLOW_MISSING_FEE_ESTIMATES") == "1",
        help="record but do not fail if qbit estimatesmartfee has no usable non-regtest estimates",
    )
    parser.add_argument(
        "--stress-height",
        "--known-height",
        dest="stress_height",
        action="append",
        type=int,
        default=[],
        help="block height to compare through qbit RPC and electrs REST; repeatable",
    )
    parser.add_argument(
        "--no-default-stress-height",
        action="store_true",
        help=f"do not automatically sample height {DEFAULT_STRESS_HEIGHT} when present",
    )
    parser.add_argument(
        "--auxpow-scan-depth",
        type=int,
        default=500,
        help="recent blocks to scan for an AuxPoW version header",
    )
    parser.add_argument(
        "--allow-missing-auxpow",
        action="store_true",
        help="do not fail if no AuxPoW block is found in the scan window",
    )
    parser.add_argument("--known-txid", action="append", default=[])
    parser.add_argument("--known-address", action="append", default=[])
    parser.add_argument(
        "--fixture-manifest",
        type=Path,
        default=FIXTURE_MANIFEST,
        help="fixture manifest path to cite in canary artifacts",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="directory for logs, temp DB, manifest, and temporary cookie file",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        default=os.environ.get("QBIT_CANARY_KEEP") == "1",
        help="keep full electrs DB/log artifacts on success; manifest is always kept",
    )
    return parser.parse_args()


class Canary:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.contract_commit = read_contract_commit()
        self.rpc_addr, self.rpc_url = normalize_rpc_addr(args.daemon_rpc_addr)
        self.node = safe_name(args.node or self.rpc_addr.split(":", 1)[0])
        self.cookie, self.cookie_source = load_cookie(args)
        self.rpc = QbitRpc(self.rpc_url, self.cookie)
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.artifacts = args.artifacts_dir or (
            ROOT / "target" / "qbit-testnet4-canary" / f"{timestamp}-{self.node}-{os.getpid()}"
        )
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = Path(tempfile.mkdtemp(prefix="qbit-canary-cookie-"))
        self.electrs_db = self.artifacts / "electrs-db"
        self.electrs_log = self.artifacts / "electrs.log"
        self.manifest_path = self.artifacts / "manifest.json"
        self.http_port = free_port()
        self.electrum_port = free_port()
        self.monitoring_port = free_port()
        self.electrs_proc: subprocess.Popen | None = None
        self.electrs_bin: Path | None = None
        self.chain_info: dict[str, object] = {}
        self.network_info: dict[str, object] = {}
        self.samples: list[dict[str, object]] = []
        self.last_checked_block: dict[str, object] | None = None
        self.last_qbit_tip: dict[str, object] = {}
        self.last_electrs_tip: dict[str, object] = {}

    def run(self) -> None:
        info(f"artifacts: {self.artifacts}")
        self.preflight_qbit()
        self.prepare_electrs()
        self.start_electrs()
        self.wait_for_tip_parity(self.args.sync_timeout)
        self.check_genesis()
        self.check_rest_and_electrum_tip()
        self.check_stress_heights()
        self.check_auxpow_sample()
        self.check_known_txs()
        self.check_known_addresses()
        self.check_fee_estimates()
        self.record_observability("initial")
        self.run_canary_window()
        self.write_manifest("passed")

    def preflight_qbit(self) -> None:
        chain = self.rpc.call("getblockchaininfo")
        network = self.rpc.call("getnetworkinfo")
        if not isinstance(chain, dict):
            raise CanaryError(f"unexpected getblockchaininfo result: {chain}")
        if not isinstance(network, dict):
            raise CanaryError(f"unexpected getnetworkinfo result: {network}")
        if chain.get("chain") != "testnet4":
            raise CanaryError(f"qbit RPC is not on testnet4: {chain.get('chain')}")
        if chain.get("pruned"):
            raise CanaryError("qbit RPC reports pruned=true; qbit-electrs needs archive blocks")
        service_names = set(network.get("localservicesnames", []))
        if "WITNESS_PRUNED" in service_names:
            raise CanaryError("qbit RPC advertises WITNESS_PRUNED; qbit-electrs needs full witnesses")
        if "ARCHIVE" not in service_names:
            raise CanaryError(
                f"qbit RPC does not advertise ARCHIVE service; localservicesnames={sorted(service_names)}"
            )
        if int(chain.get("blocks", -1)) <= 0:
            raise CanaryError(f"qbit RPC has no indexed testnet4 blocks: {chain}")
        genesis = self.rpc.call("getblockhash", [0])
        if genesis != TESTNET4_GENESIS:
            raise CanaryError(f"wrong qbit testnet4 genesis: {genesis}")
        tip_hash = chain["bestblockhash"]
        header_hex = self.rpc.call("getblockheader", [tip_hash, False])
        pure_header_hex, _ = split_qbit_rpc_header_hex(header_hex, "tip header")
        if block_hash_from_header_hex(pure_header_hex) != tip_hash:
            raise CanaryError("qbit getblockheader hash does not match best tip")
        self.rpc.call("getblock", [genesis, 1])
        archive_summary = self.rpc.call("getarchivepeers", ["summary"])
        self.chain_info = chain
        self.network_info = {
            "version": network.get("version"),
            "subversion": network.get("subversion"),
            "protocolversion": network.get("protocolversion"),
            "localservices": network.get("localservices"),
            "localservicesnames": network.get("localservicesnames"),
            "archive_peers_summary": archive_summary,
        }
        self.last_qbit_tip = {"height": int(chain["blocks"]), "hash": str(tip_hash)}
        info(f"qbit testnet4 tip {self.last_qbit_tip['height']} {self.last_qbit_tip['hash']}")

    def prepare_electrs(self) -> None:
        if self.args.build_electrs:
            run_capture(["cargo", "build", "--bin", "electrs"])
        if self.args.electrs_bin:
            self.electrs_bin = Path(self.args.electrs_bin)
        else:
            self.electrs_bin = ROOT / "target" / "debug" / "electrs"
        if not self.electrs_bin.exists():
            raise CanaryError(f"electrs binary not found at {self.electrs_bin}; use --build-electrs")

        cookie_path = self.cookie_dir / ".cookie"
        cookie_path.write_text(self.cookie)
        cookie_path.chmod(0o600)

    def electrs_command(self) -> list[object]:
        assert self.electrs_bin is not None
        return [
            self.electrs_bin,
            "-vvvv",
            "--timestamp",
            "--network",
            "qbittestnet4",
            "--daemon-dir",
            self.cookie_dir,
            "--daemon-rpc-addr",
            self.rpc_addr,
            "--jsonrpc-import",
            "--db-dir",
            self.electrs_db,
            "--http-addr",
            f"127.0.0.1:{self.http_port}",
            "--electrum-rpc-addr",
            f"127.0.0.1:{self.electrum_port}",
            "--monitoring-addr",
            f"127.0.0.1:{self.monitoring_port}",
            "--mempool-backlog-stats-ttl",
            "0",
        ]

    def start_electrs(self) -> None:
        command = self.electrs_command()
        info(f"starting qbit-electrs REST on {self.http_port}, Electrum on {self.electrum_port}")
        with self.electrs_log.open("ab") as log:
            log.write(f"$ {shlex_join(command)}\n".encode())
            self.electrs_proc = subprocess.Popen(
                [str(part) for part in command],
                cwd=str(ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def qbit_tip(self) -> dict[str, object]:
        chain = self.rpc.call("getblockchaininfo")
        if not isinstance(chain, dict):
            raise CanaryError(f"unexpected getblockchaininfo result: {chain}")
        return {"height": int(chain["blocks"]), "hash": str(chain["bestblockhash"])}

    def electrs_tip(self) -> dict[str, object]:
        return {
            "height": int(http_text(self.http_port, "/blocks/tip/height").strip()),
            "hash": http_text(self.http_port, "/blocks/tip/hash").strip(),
        }

    def monitoring_snapshot(self, label: str) -> dict[str, object]:
        metrics_text = http_text(self.monitoring_port, "/metrics", timeout=10)
        selected = selected_metric_samples(metrics_text)
        if not selected:
            raise CanaryError("monitoring scrape returned no selected electrs metrics")
        return {
            "type": "monitoring_metrics",
            "label": label,
            "addr": f"127.0.0.1:{self.monitoring_port}",
            "selected": selected,
        }

    def safe_monitoring_snapshot(self, label: str) -> dict[str, object]:
        try:
            return self.monitoring_snapshot(label)
        except Exception as exc:
            return {"type": "monitoring_metrics", "label": label, "error": str(exc)}

    def mempool_parity_snapshot(self, label: str) -> dict[str, object]:
        qbit_info = self.rpc.call("getmempoolinfo")
        qbit_txids = self.rpc.call("getrawmempool")
        rest_backlog = http_json(self.http_port, "/mempool")
        rest_txids = http_json(self.http_port, "/mempool/txids")
        if not isinstance(qbit_info, dict):
            raise CanaryError(f"qbit getmempoolinfo returned non-object: {qbit_info}")
        if not isinstance(qbit_txids, list) or not all(isinstance(txid, str) for txid in qbit_txids):
            raise CanaryError(f"qbit getrawmempool returned invalid txid list: {qbit_txids}")
        if not isinstance(rest_backlog, dict):
            raise CanaryError(f"REST /mempool returned non-object: {rest_backlog}")
        if not isinstance(rest_txids, list) or not all(isinstance(txid, str) for txid in rest_txids):
            raise CanaryError(f"REST /mempool/txids returned invalid txid list: {rest_txids}")

        qbit_size = int(qbit_info.get("size", len(qbit_txids)))
        rest_count = int(rest_backlog.get("count", len(rest_txids)))
        if qbit_size != len(qbit_txids):
            raise CanaryError(
                f"qbit mempool size mismatch: getmempoolinfo size={qbit_size} "
                f"getrawmempool len={len(qbit_txids)}"
            )
        if rest_count != qbit_size or len(rest_txids) != qbit_size:
            raise CanaryError(
                "electrs REST mempool count mismatch: "
                f"qbit={qbit_size} REST /mempool count={rest_count} "
                f"REST /mempool/txids len={len(rest_txids)}"
            )

        compare_limit = max(0, int(self.args.mempool_txid_compare_limit))
        exact_set_compared = qbit_size <= compare_limit
        missing_from_rest: list[str] = []
        extra_in_rest: list[str] = []
        if exact_set_compared:
            qbit_set = set(qbit_txids)
            rest_set = set(rest_txids)
            missing_from_rest = sorted(qbit_set - rest_set)[:20]
            extra_in_rest = sorted(rest_set - qbit_set)[:20]
            if missing_from_rest or extra_in_rest or len(qbit_set) != len(qbit_txids):
                raise CanaryError(
                    "electrs REST mempool txid set mismatch: "
                    f"missing_from_rest={missing_from_rest} extra_in_rest={extra_in_rest}"
                )

        return {
            "type": "mempool_parity",
            "label": label,
            "qbit": {
                "size": qbit_size,
                "bytes": qbit_info.get("bytes"),
                "usage": qbit_info.get("usage"),
                "loaded": qbit_info.get("loaded"),
            },
            "rest": {
                "count": rest_count,
                "txids_len": len(rest_txids),
                "vsize": rest_backlog.get("vsize"),
                "total_fee": rest_backlog.get("total_fee"),
                "fee_histogram": rest_backlog.get("fee_histogram"),
            },
            "exact_txid_set_compared": exact_set_compared,
            "txid_compare_limit": compare_limit,
            "txid_sample": rest_txids[:10],
        }

    def wait_for_mempool_parity(self, label: str, timeout: int | None = None) -> dict[str, object]:
        deadline = time.monotonic() + (timeout if timeout is not None else self.args.mempool_sync_timeout)
        last_error = ""
        while time.monotonic() < deadline:
            if self.electrs_proc and self.electrs_proc.poll() is not None:
                raise CanaryError(
                    f"electrs exited before mempool parity; see {self.electrs_log}\n"
                    + tail_file(self.electrs_log)
                )
            try:
                return self.mempool_parity_snapshot(label)
            except (CanaryError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                last_error = str(exc)
            time.sleep(2.0)
        raise CanaryError(f"timed out waiting for mempool parity: {last_error}")

    def qbit_fee_estimates(self) -> tuple[dict[int, float], dict[int, object]]:
        available: dict[int, float] = {}
        unavailable: dict[int, object] = {}
        for target in FEE_ESTIMATE_TARGETS:
            estimate = self.rpc.call("estimatesmartfee", [target])
            if not isinstance(estimate, dict):
                raise CanaryError(f"qbit estimatesmartfee({target}) returned non-object: {estimate}")
            if estimate.get("errors") is not None:
                unavailable[target] = estimate.get("errors")
                continue
            fee_rate = estimate.get("feerate")
            if fee_rate in (None, -1):
                unavailable[target] = {"feerate": fee_rate}
                continue
            fee_rate = finite_number(fee_rate, f"qbit estimatesmartfee({target}).feerate")
            if fee_rate <= 0:
                unavailable[target] = {"feerate": fee_rate}
                continue
            available[target] = fee_rate
        return available, unavailable

    def check_fee_estimates(self) -> None:
        rest_estimates = http_json(self.http_port, "/fee-estimates")
        if not isinstance(rest_estimates, dict):
            raise CanaryError(f"REST /fee-estimates returned non-object: {rest_estimates}")
        rest_numeric = {
            int(target): finite_number(value, f"REST /fee-estimates target {target}")
            for target, value in rest_estimates.items()
            if str(target).isdigit()
        }
        if len(rest_numeric) != len(rest_estimates):
            raise CanaryError(f"REST /fee-estimates returned non-numeric target keys: {rest_estimates}")

        qbit_estimates, unavailable = self.qbit_fee_estimates()
        if not qbit_estimates and not self.args.allow_missing_fee_estimates:
            raise CanaryError(
                "qbit estimatesmartfee returned no usable estimates; "
                "rerun with --allow-missing-fee-estimates to record this as a known testnet state"
            )

        rest_matches: dict[str, float] = {}
        for target, qbit_btc_kb in qbit_estimates.items():
            if target not in rest_numeric:
                raise CanaryError(f"REST /fee-estimates missing qbit target {target}")
            expected_sat_vb = qbit_btc_kb * BTC_PER_KB_TO_SAT_PER_VB
            actual_sat_vb = rest_numeric[target]
            if abs(actual_sat_vb - expected_sat_vb) > FEE_ESTIMATE_SAT_VB_TOLERANCE:
                raise CanaryError(
                    f"REST /fee-estimates target {target} mismatch: "
                    f"{actual_sat_vb} sat/vB != qbit {expected_sat_vb} sat/vB"
                )
            rest_matches[str(target)] = actual_sat_vb

        electrum_matches: dict[str, float] = {}
        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=10) as sock:
            file = sock.makefile("rwb")
            relayfee = finite_number(
                electrum_request(file, 200, "blockchain.relayfee", []),
                "Electrum blockchain.relayfee",
            )
            network_info = self.rpc.call("getnetworkinfo")
            if not isinstance(network_info, dict):
                raise CanaryError(f"qbit getnetworkinfo returned non-object: {network_info}")
            qbit_relayfee = finite_number(network_info.get("relayfee"), "qbit getnetworkinfo.relayfee")
            if abs(relayfee - qbit_relayfee) > FEE_ESTIMATE_BTC_KB_TOLERANCE:
                raise CanaryError(f"Electrum relayfee mismatch: {relayfee} != qbit {qbit_relayfee}")

            request_id = 201
            for target, qbit_btc_kb in qbit_estimates.items():
                fee_rate = finite_number(
                    electrum_request(file, request_id, "blockchain.estimatefee", [target]),
                    f"Electrum blockchain.estimatefee({target})",
                )
                request_id += 1
                if abs(fee_rate - qbit_btc_kb) > FEE_ESTIMATE_BTC_KB_TOLERANCE:
                    raise CanaryError(
                        f"Electrum estimatefee({target}) mismatch: {fee_rate} BTC/kB != "
                        f"qbit {qbit_btc_kb} BTC/kB"
                    )
                electrum_matches[str(target)] = fee_rate

        self.samples.append(
            {
                "type": "fee_estimates",
                "targets": FEE_ESTIMATE_TARGETS,
                "available_targets": sorted(qbit_estimates),
                "unavailable_targets": {str(target): value for target, value in unavailable.items()},
                "rest_sat_vb": rest_matches,
                "electrum_btc_per_kb": electrum_matches,
                "rest_only_targets": sorted(set(rest_numeric) - set(qbit_estimates)),
            }
        )

    def record_observability(self, label: str, iteration: int | None = None) -> None:
        sample: dict[str, object] = {
            "type": "canary_observability",
            "label": label,
            "qbit_tip": self.last_qbit_tip,
            "electrs_tip": self.last_electrs_tip,
            "mempool": self.wait_for_mempool_parity(label),
            "monitoring": self.monitoring_snapshot(label),
        }
        if iteration is not None:
            sample["iteration"] = iteration
        self.samples.append(sample)

    def wait_for_tip_parity(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.electrs_proc and self.electrs_proc.poll() is not None:
                raise CanaryError(
                    f"electrs exited before tip parity; see {self.electrs_log}\n"
                    + tail_file(self.electrs_log)
                )
            try:
                qbit_tip = self.qbit_tip()
                electrs_tip = self.electrs_tip()
                self.last_qbit_tip = qbit_tip
                self.last_electrs_tip = electrs_tip
                if qbit_tip == electrs_tip:
                    return
                last_error = f"qbit={qbit_tip}, electrs={electrs_tip}"
            except (CanaryError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                last_error = str(exc)
            time.sleep(2.0)
        raise CanaryError(f"timed out waiting for electrs tip parity: {last_error}")

    def check_genesis(self) -> None:
        rest_genesis = http_text(self.http_port, "/block-height/0").strip()
        if rest_genesis != TESTNET4_GENESIS:
            raise CanaryError(f"REST genesis mismatch: {rest_genesis} != {TESTNET4_GENESIS}")
        self.samples.append({"type": "genesis", "height": 0, "hash": rest_genesis})

    def check_rest_and_electrum_tip(self) -> None:
        self.check_block_height(int(self.last_qbit_tip["height"]), "tip")
        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=10) as sock:
            file = sock.makefile("rwb")
            version = electrum_request(
                file,
                1,
                "server.version",
                ["qbit-electrs-canary", "1.4"],
            )
            if (
                not isinstance(version, list)
                or len(version) != 2
                or not str(version[0]).startswith("qbit-electrs ")
                or str(version[1]) != "1.4"
            ):
                raise CanaryError(f"unexpected Electrum server.version result: {version}")
            header = electrum_request(file, 2, "blockchain.headers.subscribe", [])
            if not isinstance(header, dict):
                raise CanaryError(f"unexpected Electrum header result: {header}")
            if int(header.get("height", -1)) != int(self.last_qbit_tip["height"]):
                raise CanaryError(f"Electrum height mismatch: {header}")
            if block_hash_from_header_hex(str(header.get("hex", ""))) != self.last_qbit_tip["hash"]:
                raise CanaryError(f"Electrum header hash mismatch: {header}")

    def check_block_height(self, height: int, label: str) -> dict[str, object]:
        qbit_hash = self.rpc.call("getblockhash", [height])
        self.last_checked_block = {"height": height, "hash": qbit_hash, "label": label}
        rest_hash = http_text(self.http_port, f"/block-height/{height}").strip()
        if rest_hash != qbit_hash:
            raise CanaryError(f"REST block-height mismatch at {height}: {rest_hash} != {qbit_hash}")

        qbit_header = self.rpc.call("getblockheader", [qbit_hash, False])
        qbit_pure_header, qbit_auxpow_payload = split_qbit_rpc_header_hex(
            qbit_header,
            f"header at {height}",
        )
        rest_header = http_text(self.http_port, f"/block/{qbit_hash}/header").strip()
        if rest_header.lower() != qbit_pure_header:
            raise CanaryError(f"REST header mismatch at {height}")
        if block_hash_from_header_hex(rest_header) != qbit_hash:
            raise CanaryError(f"REST header hash mismatch at {height}")

        qbit_block = self.rpc.call("getblock", [qbit_hash, 1])
        rest_txids = http_json(self.http_port, f"/block/{qbit_hash}/txids")
        qbit_txids = qbit_block.get("tx", []) if isinstance(qbit_block, dict) else []
        if rest_txids != qbit_txids:
            raise CanaryError(f"txid list mismatch at {height}: electrs={rest_txids} qbit={qbit_txids}")

        header_json = self.rpc.call("getblockheader", [qbit_hash, True])
        version = int(header_json.get("version", 0)) if isinstance(header_json, dict) else 0
        auxpow = has_auxpow_flag(version)
        if auxpow and not qbit_auxpow_payload:
            raise CanaryError(f"AuxPoW header at {height} has no serialized AuxPoW payload")
        if qbit_auxpow_payload and not auxpow:
            raise CanaryError(f"non-AuxPoW header at {height} has trailing payload bytes")
        sample = {
            "type": "block",
            "label": label,
            "height": height,
            "hash": qbit_hash,
            "tx_count": len(rest_txids),
            "auxpow": auxpow,
            "rpc_header_bytes": len(qbit_header) // 2,
            "rpc_auxpow_payload_bytes": len(qbit_auxpow_payload) // 2,
            "version": version,
        }
        self.samples.append(sample)
        return sample

    def check_stress_heights(self) -> None:
        heights = list(dict.fromkeys(self.args.stress_height))
        if not self.args.no_default_stress_height:
            heights.append(DEFAULT_STRESS_HEIGHT)
        best_height = int(self.last_qbit_tip["height"])
        for height in sorted(set(heights)):
            if height > best_height:
                info(f"skipping stress height {height}; qbit tip is {best_height}")
                continue
            info(f"checking stress block height {height}")
            self.check_block_height(height, "stress")

    def check_auxpow_sample(self) -> None:
        best_height = int(self.last_qbit_tip["height"])
        floor = max(0, best_height - self.args.auxpow_scan_depth)
        for height in range(best_height, floor - 1, -1):
            blockhash = self.rpc.call("getblockhash", [height])
            header = self.rpc.call("getblockheader", [blockhash, True])
            version = int(header.get("version", 0)) if isinstance(header, dict) else 0
            if has_auxpow_flag(version):
                info(f"checking AuxPoW block height {height}")
                self.check_block_height(height, "auxpow")
                return
        if self.args.allow_missing_auxpow:
            self.samples.append(
                {
                    "type": "auxpow_scan",
                    "found": False,
                    "scan_depth": self.args.auxpow_scan_depth,
                }
            )
            return
        raise CanaryError(f"no AuxPoW block found in last {self.args.auxpow_scan_depth} blocks")

    def check_known_txs(self) -> None:
        for txid in self.args.known_txid:
            status = http_json(self.http_port, f"/tx/{txid}/status")
            if not isinstance(status, dict) or "confirmed" not in status:
                raise CanaryError(f"REST status missing for known tx {txid}: {status}")
            sample = {"type": "known_tx", "txid": txid, "rest_status": status}
            try:
                qbit_hex = self.rpc.call("getrawtransaction", [txid, False])
            except CanaryError as exc:
                raise CanaryError(f"qbit raw tx unavailable for known tx {txid}: {exc}") from exc
            rest_hex = http_text(self.http_port, f"/tx/{txid}/hex").strip()
            if not isinstance(qbit_hex, str) or rest_hex.lower() != qbit_hex.lower():
                raise CanaryError(f"known tx hex mismatch for {txid}")
            sample["hex_parity"] = True
            self.samples.append(sample)

    def check_known_addresses(self) -> None:
        for address in self.args.known_address:
            validation = self.rpc.call("validateaddress", [address])
            if not isinstance(validation, dict) or not validation.get("isvalid"):
                raise CanaryError(f"qbitd does not validate known address {address}: {validation}")
            script_hex = validation.get("scriptPubKey")
            if not isinstance(script_hex, str):
                raise CanaryError(f"validateaddress did not return scriptPubKey for {address}")
            script_hash = bytes.fromhex(sha256_hex(bytes.fromhex(script_hex)))[::-1].hex()
            rest_stats = http_json(self.http_port, f"/address/{address}")
            rest_history_count = int(rest_stats.get("chain_stats", {}).get("tx_count", 0)) + int(
                rest_stats.get("mempool_stats", {}).get("tx_count", 0)
            )
            with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=10) as sock:
                file = sock.makefile("rwb")
                electrum_history = electrum_request(
                    file,
                    100,
                    "blockchain.scripthash.get_history",
                    [script_hash],
                )
            if not isinstance(electrum_history, list):
                raise CanaryError(f"Electrum history for known address is not a list: {electrum_history}")
            if rest_history_count != len(electrum_history):
                raise CanaryError(
                    f"known address history count mismatch for {address}: "
                    f"REST={rest_history_count} Electrum={len(electrum_history)}"
                )
            self.samples.append(
                {
                    "type": "known_address",
                    "address": address,
                    "script_hash": script_hash,
                    "rest_chain_tx_count": rest_stats.get("chain_stats", {}).get("tx_count"),
                    "rest_mempool_tx_count": rest_stats.get("mempool_stats", {}).get("tx_count"),
                    "electrum_history_count": len(electrum_history),
                }
            )

    def run_canary_window(self) -> None:
        if self.args.duration <= 0:
            return
        deadline = time.monotonic() + self.args.duration
        iteration = 0
        while time.monotonic() < deadline:
            iteration += 1
            self.wait_for_tip_parity(max(self.args.interval, 30))
            self.record_observability(f"window-{iteration}", iteration)
            sleep_for = min(self.args.interval, max(0, deadline - time.monotonic()))
            if sleep_for > 0:
                time.sleep(sleep_for)

    def failure_context(self, error: str) -> dict[str, object]:
        context: dict[str, object] = {"error": error}
        try:
            context["qbit_tip"] = self.qbit_tip()
        except Exception as exc:
            context["qbit_tip_error"] = str(exc)
        try:
            context["electrs_tip"] = self.electrs_tip()
        except Exception as exc:
            context["electrs_tip_error"] = str(exc)
        if self.electrs_proc is not None:
            context["monitoring"] = self.safe_monitoring_snapshot("failure")
        context["electrs_log_tail"] = tail_file(self.electrs_log)
        context["last_checked_block"] = self.last_checked_block
        return context

    def write_manifest(self, status: str, error: str | None = None) -> None:
        manifest = {
            "status": status,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "contract": {
                "document": "doc/qbit-contract.md",
                "qbit_repository": "qbit-reference",
                "qbit_commit": self.contract_commit,
                "fixture_manifest": str(self.args.fixture_manifest.relative_to(ROOT))
                if self.args.fixture_manifest.is_absolute()
                and self.args.fixture_manifest.is_relative_to(ROOT)
                else str(self.args.fixture_manifest),
                "issues": ["#12", "#14", "#21"],
            },
            "qbit_testnet4": {
                "node": self.node,
                "daemon_rpc_addr": self.rpc_addr,
                "cookie_source": self.cookie_source,
                "genesis": TESTNET4_GENESIS,
                "initial_chain_info": self.chain_info,
                "network_info": self.network_info,
            },
            "electrs": {
                "http_addr": f"127.0.0.1:{self.http_port}",
                "electrum_addr": f"127.0.0.1:{self.electrum_port}",
                "monitoring_addr": f"127.0.0.1:{self.monitoring_port}",
                "db_dir": str(self.electrs_db),
                "log": str(self.electrs_log),
                "cookie_dir": "temporary directory removed at exit",
            },
            "samples": self.samples,
        }
        if error:
            manifest["failure"] = self.failure_context(error)
        self.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        info(f"manifest: {self.manifest_path}")

    def stop(self) -> None:
        if self.electrs_proc and self.electrs_proc.poll() is None:
            try:
                os.killpg(self.electrs_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                self.electrs_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.electrs_proc.pid, signal.SIGKILL)
                self.electrs_proc.wait(timeout=10)
        shutil.rmtree(self.cookie_dir, ignore_errors=True)


def main() -> int:
    args = parse_args()
    try:
        canary = Canary(args)
    except Exception as exc:
        print(f"[qbit-canary] ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        canary.run()
    except Exception as exc:
        error = str(exc)
        print(f"[qbit-canary] ERROR: {error}", file=sys.stderr)
        try:
            canary.write_manifest("failed", error)
        finally:
            canary.stop()
        return 1
    canary.stop()
    if not args.keep_artifacts:
        shutil.rmtree(canary.electrs_db, ignore_errors=True)
    info(f"success artifacts kept at {canary.artifacts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
