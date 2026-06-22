#!/usr/bin/env python3
"""Run a local qbit regtest smoke harness against qbit-electrs.

This intentionally keeps the scenario small: qbitd regtest, mature P2MR
coinbase outputs,
qbit-electrs sync, REST tip/header/address parity, P2MR mempool spend coverage,
and basic Electrum qbit checks. Reorg and live testnet scenarios can build on
this lifecycle.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "doc" / "qbit-contract.md"
DEFAULT_QBIT_SOURCE = ROOT / ".context" / "qbit-source"
DEFAULT_QBIT_REPO = os.environ.get(
    "QBIT_SOURCE_REPO_URL", "https://github.com/Qbit-Org/qbit.git"
)
REGTEST_GENESIS = "0ee96aa77c4b600850e349344fa21b107e805f5370ddc7a6189db12cf69acce6"
COINBASE_MATURITY = 1000
SPEND_AMOUNT_BTC = "1.0"
SPEND_AMOUNT_SATS = 100_000_000
QBIT_ESTIMATEFEE_TARGETS = [1, 2, 3, 6, 12, 25, 144, 504, 1008]
QBIT_REST_FEE_ESTIMATE_TARGETS = list(range(1, 26)) + [144, 504, 1008]
PURE_BLOCK_HEADER_HEX_LEN = 160


class HarnessError(Exception):
    pass


def info(message: str) -> None:
    print(f"[qbit-harness] {message}", flush=True)


def display_arg(part: object) -> str:
    text = str(part)
    if len(text) <= 180:
        return text
    omitted = len(text) - 130
    return f"{text[:90]}...<snip {omitted} chars>...{text[-40:]}"


def shlex_join(command: list[object]) -> str:
    return shlex.join(display_arg(part) for part in command)


def run_capture(command: list[object], cwd: Path | None = None, check: bool = True) -> str:
    info(f"+ {shlex_join(command)}")
    proc = subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise HarnessError(
            "command failed with exit code "
            f"{proc.returncode}: {shlex_join(command)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout.strip()


def run_ok(command: list[object], cwd: Path | None = None) -> bool:
    info(f"+ {shlex_join(command)}")
    proc = subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0


def run_logged(command: list[object], log_path: Path, cwd: Path | None = None) -> None:
    info(f"+ {shlex_join(command)}")
    with log_path.open("ab") as log:
        log.write(f"$ {shlex_join(command)}\n".encode())
        proc = subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if proc.returncode != 0:
        raise HarnessError(
            f"command failed with exit code {proc.returncode}: {shlex_join(command)}\n"
            f"see log: {log_path}"
        )


def read_contract_commit() -> str:
    text = CONTRACT_PATH.read_text()
    match = re.search(
        r"qbit source ref: `(?:origin/main|pinned snapshot)` at `([0-9a-f]{40})`",
        text,
    )
    if not match:
        raise HarnessError(f"could not find qbit source ref in {CONTRACT_PATH}")
    return match.group(1)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def json_loads_loose(output: str):
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def block_hash_from_header_hex(header_hex: str) -> str:
    header = bytes.fromhex(header_hex)
    first = hashlib.sha256(header).digest()
    return hashlib.sha256(first).digest()[::-1].hex()


def double_sha256(data: bytes) -> bytes:
    first = hashlib.sha256(data).digest()
    return hashlib.sha256(first).digest()


def electrum_merkle_root_from_branch(hash_hex: str, height: int, branch: list[object]) -> str:
    root = bytes.fromhex(hash_hex)[::-1]
    index = height
    for item in branch:
        if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item):
            raise HarnessError(f"invalid Electrum header proof branch item: {item}")
        sibling = bytes.fromhex(item)[::-1]
        if index % 2 == 0:
            root = double_sha256(root + sibling)
        else:
            root = double_sha256(sibling + root)
        index //= 2
    return root[::-1].hex()


def tail_file(path: Path, max_bytes: int = 16000) -> str:
    if not path.exists():
        return f"{path} does not exist"
    data = path.read_bytes()
    return data[-max_bytes:].decode(errors="replace")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def clamp_child_nofile() -> None:
    """Give qbitd a finite RLIMIT_NOFILE.

    Some macOS shells report RLIM_INFINITY for the soft descriptor limit. qbitd
    stores that value in an int during startup, which can become -1. Clamp only
    the child process so the harness parent keeps the user's shell limits.
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 4096
        if hard != resource.RLIM_INFINITY:
            target = min(target, int(hard))
        if soft == resource.RLIM_INFINITY or int(soft) != target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass


def http_bytes(port: int, path: str, timeout: float = 5.0) -> bytes:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def http_text(port: int, path: str, timeout: float = 5.0) -> str:
    return http_bytes(port, path, timeout).decode()


def http_json(port: int, path: str, timeout: float = 5.0):
    return json.loads(http_text(port, path, timeout))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run qbitd regtest -> qbit-electrs smoke/parity harness.",
    )
    parser.add_argument(
        "--qbit-source",
        default=os.environ.get("QBIT_SOURCE_DIR", "auto"),
        help="qbit source checkout, or 'auto' for .context/qbit-source",
    )
    parser.add_argument(
        "--qbit-repo",
        default=DEFAULT_QBIT_REPO,
        help="qbit git repository URL used only when --qbit-source auto must clone",
    )
    parser.add_argument("--qbitd", default=os.environ.get("QBITD"))
    parser.add_argument("--qbit-cli", default=os.environ.get("QBIT_CLI"))
    parser.add_argument("--electrs-bin", default=os.environ.get("ELECTRS_BIN"))
    parser.add_argument(
        "--build-qbit",
        action="store_true",
        help="configure/build qbitd and qbit-cli from the contract source if missing",
    )
    parser.add_argument(
        "--build-electrs",
        action="store_true",
        help="force cargo build --bin electrs before running",
    )
    parser.add_argument(
        "--checkout-qbit-ref",
        action="store_true",
        help="allow the script to detach the qbit source checkout to the contract ref",
    )
    parser.add_argument(
        "--mine-blocks",
        type=int,
        default=COINBASE_MATURITY + 1,
        help="regtest blocks to mine before starting electrs",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="seconds to wait for qbitd/electrs readiness",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        default=os.environ.get("QBIT_HARNESS_KEEP") == "1",
        help="keep target/qbit-regtest-harness artifacts on success",
    )
    parser.add_argument(
        "--export-qbit-fixtures",
        action="store_true",
        help=(
            "write a qbitd-generated P2MR tx/block/RPC fixture bundle under the "
            "artifact directory and keep artifacts on success"
        ),
    )
    return parser.parse_args()


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.contract_commit = read_contract_commit()
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.artifacts = ROOT / "target" / "qbit-regtest-harness" / f"{timestamp}-{os.getpid()}"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.qbit_datadir = self.artifacts / "qbitd"
        self.electrs_db = self.artifacts / "electrs-db"
        self.qbit_log = self.artifacts / "qbitd.log"
        self.electrs_log = self.artifacts / "electrs.log"
        self.build_log = self.artifacts / "build.log"
        self.qbit_proc: subprocess.Popen | None = None
        self.electrs_proc: subprocess.Popen | None = None
        self.qbit_source: Path | None = None
        self.qbitd: Path | None = None
        self.qbit_cli: Path | None = None
        self.electrs_bin: Path | None = None
        self.rpc_port = free_port()
        self.p2p_port = free_port()
        self.http_port = free_port()
        self.electrum_port = free_port()
        self.monitoring_port = free_port()
        self.wallet_name = "harness"
        self.p2mr_address = ""
        self.spend_address = ""
        self.spend_script_hash = ""
        self.spend_electrum_scripthash = ""
        self.spend_txid = ""
        self.spend_tx_hex = ""
        self.spend_prevout_txid = ""
        self.spend_prevout_vout = 0
        self.spend_recipient_vout = 0
        self.spend_confirmed_height = 0
        self.reorged_block_hash = ""
        self.reorged_to_height = 0
        self.reorged_to_hash = ""
        self.best_hash = ""
        self.best_height = 0
        self.spend_mempool_entry: dict | None = None
        self.spend_mempool_txids: list[str] | None = None
        self.spend_block_hex = ""
        self.spend_block_json: dict | None = None
        self.spend_block_header_json: dict | None = None
        self.spend_block_txids: list[str] = []
        self.spend_tx_block_pos = 0
        self.spend_tx_confirmed_verbose: dict | None = None
        self.mempool_aggregate_surfaces: dict[str, dict[str, object]] = {}
        self.rest_recent_spend_matches = 0
        self.electrum_relayfee_btc_kb = 0.0
        self.electrum_estimatefee_targets: dict[str, float] = {}
        self.rest_fee_estimate_targets: dict[str, float] = {}
        self.electrum_auxpow_header_methods: list[str] = []
        self.electrum_auxpow_header_facts: dict[str, object] = {}
        self.auxpow_block_hash = ""
        self.auxpow_block_height = 0
        self.auxpow_block_hex = ""
        self.auxpow_block_json: dict | None = None
        self.auxpow_extended_header_hex = ""
        self.auxpow_pure_header_hex = ""
        self.auxpow_txids: list[str] = []
        self.fixture_export_root: Path | None = None
        self.fixture_export_manifest: Path | None = None

    def run(self) -> None:
        info(f"artifacts: {self.artifacts}")
        self.resolve_qbit_source()
        self.resolve_qbit_binaries()
        self.resolve_electrs_binary()
        self.start_qbitd()
        self.prepare_chain()
        self.start_electrs()
        self.wait_for_electrs_tip()
        self.check_rest_parity()
        self.check_electrum_parity()
        self.check_electrum_fee_methods()
        self.create_p2mr_mempool_spend()
        self.wait_for_electrs_mempool_spend()
        self.check_rest_mempool_spend()
        self.check_electrum_mempool_spend()
        self.check_mempool_aggregate_surfaces("initial_mempool")
        self.mine_mempool_spend()
        self.wait_for_electrs_tip()
        self.wait_for_electrs_confirmed_spend()
        self.check_rest_confirmed_spend()
        self.check_electrum_confirmed_spend()
        self.check_mempool_aggregate_empty("confirmed_after_mine")
        self.invalidate_confirmed_spend_block()
        self.wait_for_electrs_tip()
        self.wait_for_electrs_mempool_spend()
        self.check_rest_mempool_spend()
        self.check_electrum_mempool_spend()
        self.check_mempool_aggregate_surfaces("post_reorg_mempool")
        self.reconsider_confirmed_spend_block()
        self.wait_for_electrs_tip()
        self.wait_for_electrs_confirmed_spend()
        self.check_rest_confirmed_spend()
        self.check_electrum_confirmed_spend()
        self.check_mempool_aggregate_empty("confirmed_after_reconsider")
        self.submit_auxpow_block()
        self.wait_for_electrs_tip()
        self.check_rest_auxpow_block()
        self.check_electrum_auxpow_block_headers()
        if self.args.export_qbit_fixtures:
            self.write_qbit_fixture_exports()
        self.write_manifest()
        info("harness passed")

    def using_explicit_qbit_binaries(self) -> bool:
        if bool(self.args.qbitd) != bool(self.args.qbit_cli):
            raise HarnessError("set both QBITD and QBIT_CLI, or neither")
        return bool(self.args.qbitd and self.args.qbit_cli)

    def resolve_qbit_source(self) -> None:
        if self.using_explicit_qbit_binaries() and self.args.qbit_source == "auto":
            self.qbit_source = None
            return

        source_arg = self.args.qbit_source
        source = DEFAULT_QBIT_SOURCE if source_arg == "auto" else Path(source_arg).expanduser()
        if not source.exists():
            if source_arg != "auto":
                raise HarnessError(f"qbit source does not exist: {source}")
            if not self.args.qbit_repo:
                raise HarnessError(
                    "qbit source auto-clone needs --qbit-repo or QBIT_SOURCE_REPO_URL"
                )
            source.parent.mkdir(parents=True, exist_ok=True)
            run_capture(["git", "clone", self.args.qbit_repo, source], cwd=ROOT)
            run_capture(["git", "checkout", "--detach", self.contract_commit], cwd=source)

        is_work_tree = run_capture(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=source,
            check=False,
        )
        if is_work_tree != "true":
            raise HarnessError(f"qbit source is not a git checkout: {source}")

        # Best-effort drift check: comparing the checkout's origin/main to the
        # pinned contract commit is purely informational. In CI the qbit source
        # is checked out at the exact contract commit with credentials stripped
        # (actions/checkout persist-credentials: false), so `git fetch` cannot
        # authenticate and a failure here must not abort the harness. The
        # authoritative guards are the contract-commit presence and HEAD checks
        # below, both satisfied by the pinned checkout.
        if run_ok(["git", "fetch", "origin", "main"], cwd=source):
            origin_main = run_capture(
                ["git", "rev-parse", "origin/main"], cwd=source, check=False
            )
            if origin_main and origin_main != self.contract_commit:
                info(
                    "qbit origin/main differs from doc/qbit-contract.md; "
                    f"using pinned contract commit {self.contract_commit}"
                )
        else:
            info(
                "skipping qbit origin/main drift check (git fetch unavailable, "
                "e.g. a credential-less CI checkout); relying on the pinned "
                f"contract commit {self.contract_commit}"
            )
        commit_ref = f"{self.contract_commit}^{{commit}}"
        if not run_ok(["git", "cat-file", "-e", commit_ref], cwd=source):
            run_capture(["git", "fetch", "origin", self.contract_commit], cwd=source)
        if not run_ok(["git", "cat-file", "-e", commit_ref], cwd=source):
            raise HarnessError(
                f"qbit contract commit is not present in {source}: {self.contract_commit}"
            )

        head = run_capture(["git", "rev-parse", "HEAD"], cwd=source)
        if head != self.contract_commit:
            if not self.args.checkout_qbit_ref:
                raise HarnessError(
                    f"qbit source HEAD is {head}, expected {self.contract_commit}. "
                    "Use --checkout-qbit-ref or point QBITD/QBIT_CLI at binaries built "
                    "from the contract ref."
                )
            status = run_capture(["git", "status", "--porcelain"], cwd=source)
            if status:
                raise HarnessError(
                    f"qbit source has local changes; refusing checkout:\n{status}"
                )
            run_capture(["git", "checkout", "--detach", self.contract_commit], cwd=source)

        self.qbit_source = source

    def resolve_qbit_binaries(self) -> None:
        explicit_binaries = self.using_explicit_qbit_binaries()
        if explicit_binaries:
            qbitd = Path(self.args.qbitd).expanduser()
            qbit_cli = Path(self.args.qbit_cli).expanduser()
        else:
            assert self.qbit_source is not None
            qbitd = self.qbit_source / "build" / "bin" / "qbitd"
            qbit_cli = self.qbit_source / "build" / "bin" / "qbit-cli"

        if not qbitd.exists() or not qbit_cli.exists():
            if explicit_binaries:
                raise HarnessError(
                    f"explicit qbitd/qbit-cli not found: {qbitd}, {qbit_cli}"
                )
            if not self.args.build_qbit:
                raise HarnessError(
                    "qbitd/qbit-cli not found. Set QBITD and QBIT_CLI, or run with "
                    "--qbit-source auto --build-qbit."
                )
            jobs = os.cpu_count() or 2
            build_dir = self.qbit_source / "build"
            run_logged(
                [
                    "cmake",
                    "-S",
                    self.qbit_source,
                    "-B",
                    build_dir,
                    "-DBUILD_DAEMON=ON",
                    "-DBUILD_CLI=ON",
                    "-DENABLE_WALLET=ON",
                    "-DBUILD_GUI=OFF",
                    "-DBUILD_TESTS=OFF",
                    "-DENABLE_IPC=OFF",
                ],
                self.build_log,
            )
            run_logged(
                [
                    "cmake",
                    "--build",
                    build_dir,
                    "--target",
                    "qbitd",
                    "qbit-cli",
                    "-j",
                    str(jobs),
                ],
                self.build_log,
            )

        if not os.access(qbitd, os.X_OK):
            raise HarnessError(f"qbitd is not executable: {qbitd}")
        if not os.access(qbit_cli, os.X_OK):
            raise HarnessError(f"qbit-cli is not executable: {qbit_cli}")

        self.qbitd = qbitd
        self.qbit_cli = qbit_cli

    def resolve_electrs_binary(self) -> None:
        if self.args.electrs_bin:
            electrs_bin = Path(self.args.electrs_bin).expanduser()
        else:
            electrs_bin = ROOT / "target" / "debug" / "electrs"
            if self.args.build_electrs or not electrs_bin.exists():
                run_logged(["cargo", "build", "--bin", "electrs"], self.build_log, cwd=ROOT)

        if not electrs_bin.exists() or not os.access(electrs_bin, os.X_OK):
            raise HarnessError(f"electrs binary is not executable: {electrs_bin}")
        self.electrs_bin = electrs_bin

    def qbit_cli_command(self, *args: object, wallet: str | None = None) -> list[object]:
        assert self.qbit_cli is not None
        command: list[object] = [
            self.qbit_cli,
            "-regtest",
            f"-datadir={self.qbit_datadir}",
            "-rpcconnect=127.0.0.1",
            f"-rpcport={self.rpc_port}",
        ]
        if wallet:
            command.append(f"-rpcwallet={wallet}")
        command.extend(args)
        return command

    def qbit_cli_call(self, *args: object, wallet: str | None = None):
        output = run_capture(self.qbit_cli_command(*args, wallet=wallet), cwd=ROOT)
        return json_loads_loose(output)

    def new_p2mr_address(self) -> str:
        address = self.qbit_cli_call("getnewaddress", "", "p2mr", wallet=self.wallet_name)
        if not isinstance(address, str) or not address.startswith("qbrt1"):
            raise HarnessError(f"wallet returned non-regtest P2MR address: {address}")

        validation = self.qbit_cli_call("validateaddress", address)
        if not isinstance(validation, dict) or not validation.get("isvalid"):
            raise HarnessError(f"qbitd does not validate generated P2MR address: {validation}")
        return address

    def p2mr_script_hashes(self, address: str) -> tuple[str, str]:
        validation = self.qbit_cli_call("validateaddress", address)
        if not isinstance(validation, dict) or not validation.get("isvalid"):
            raise HarnessError(f"qbitd does not validate P2MR address: {validation}")
        if validation.get("witness_version") != 2:
            raise HarnessError(f"expected witness version 2 P2MR address: {validation}")
        script_hex = validation.get("scriptPubKey")
        if not isinstance(script_hex, str):
            raise HarnessError(f"validateaddress did not return scriptPubKey: {validation}")
        try:
            script = bytes.fromhex(script_hex)
        except ValueError as exc:
            raise HarnessError(f"invalid P2MR scriptPubKey hex: {script_hex}") from exc
        if len(script) != 34 or script[:2] != b"\x52\x20":
            raise HarnessError(f"unexpected P2MR scriptPubKey shape: {script_hex}")
        script_hash = hashlib.sha256(script).digest()
        # REST stores SHA256(script) bytes directly; Electrum RPC passes them reversed.
        return script_hash.hex(), script_hash[::-1].hex()

    def start_qbitd(self) -> None:
        assert self.qbitd is not None
        self.qbit_datadir.mkdir(parents=True, exist_ok=True)
        command = [
            self.qbitd,
            "-regtest",
            f"-datadir={self.qbit_datadir}",
            "-server=1",
            "-disablewallet=0",
            "-printtoconsole",
            "-listen=0",
            "-listenonion=0",
            "-dnsseed=0",
            "-fixedseeds=0",
            "-natpmp=0",
            "-discover=0",
            "-p2mronly=1",
            "-auxpowtemplateexpiry=1",
            "-auxpowtemplatecachelimit=12",
            "-fallbackfee=0.0001",
            "-rpcbind=127.0.0.1",
            "-rpcallowip=127.0.0.1",
            f"-rpcport={self.rpc_port}",
            f"-port={self.p2p_port}",
        ]
        info(f"starting qbitd on RPC port {self.rpc_port}")
        with self.qbit_log.open("ab") as log:
            log.write(f"$ {shlex_join(command)}\n".encode())
            self.qbit_proc = subprocess.Popen(
                [str(part) for part in command],
                cwd=str(ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=clamp_child_nofile,
            )
        self.wait_for_qbit_rpc()

    def wait_for_qbit_rpc(self) -> None:
        deadline = time.monotonic() + self.args.timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.qbit_proc and self.qbit_proc.poll() is not None:
                raise HarnessError(
                    f"qbitd exited before RPC was ready; see {self.qbit_log}\n"
                    + tail_file(self.qbit_log)
                )
            try:
                info_value = self.qbit_cli_call("getblockchaininfo")
                if isinstance(info_value, dict) and info_value.get("chain") == "regtest":
                    return
            except HarnessError as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise HarnessError(f"timed out waiting for qbit RPC: {last_error}")

    def prepare_chain(self) -> None:
        genesis = self.qbit_cli_call("getblockhash", 0)
        if genesis != REGTEST_GENESIS:
            raise HarnessError(f"wrong qbit regtest genesis: {genesis}")

        self.qbit_cli_call(
            "-named",
            "createwallet",
            f"wallet_name={self.wallet_name}",
            "load_on_startup=true",
        )
        if self.args.mine_blocks <= COINBASE_MATURITY:
            raise HarnessError(
                f"--mine-blocks must exceed qbit coinbase maturity ({COINBASE_MATURITY})"
            )

        address = self.new_p2mr_address()
        padding_address = self.new_p2mr_address()
        info("mining one probe qbit regtest block to a P2MR address")
        self.qbit_cli_call("generatetoaddress", 1, address)
        padding_blocks = self.args.mine_blocks - 1
        info(f"mining {padding_blocks} maturity-padding blocks to a separate P2MR address")
        self.qbit_cli_call("generatetoaddress", padding_blocks, padding_address)
        chain = self.qbit_cli_call("getblockchaininfo")
        if not isinstance(chain, dict):
            raise HarnessError(f"unexpected getblockchaininfo result: {chain}")
        if chain.get("pruned"):
            raise HarnessError("qbitd regtest unexpectedly reports pruned=true")
        self.best_height = int(chain["blocks"])
        self.best_hash = str(chain["bestblockhash"])
        self.p2mr_address = address

    def start_electrs(self) -> None:
        assert self.electrs_bin is not None
        command = [
            self.electrs_bin,
            "-vvvv",
            "--network",
            "qbitregtest",
            "--daemon-dir",
            self.qbit_datadir / "regtest",
            "--daemon-rpc-addr",
            f"127.0.0.1:{self.rpc_port}",
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

    def wait_for_electrs_tip(self) -> None:
        deadline = time.monotonic() + self.args.timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.electrs_proc and self.electrs_proc.poll() is not None:
                raise HarnessError(
                    f"electrs exited before syncing; see {self.electrs_log}\n"
                    + tail_file(self.electrs_log)
                )
            try:
                height = int(http_text(self.http_port, "/blocks/tip/height").strip())
                tip_hash = http_text(self.http_port, "/blocks/tip/hash").strip()
                if height == self.best_height and tip_hash == self.best_hash:
                    return
                last_error = f"height={height}, hash={tip_hash}"
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                last_error = str(exc)
            time.sleep(1.0)
        raise HarnessError(f"timed out waiting for electrs tip parity: {last_error}")

    def check_rest_parity(self) -> None:
        rest_height = int(http_text(self.http_port, "/blocks/tip/height").strip())
        rest_hash = http_text(self.http_port, "/blocks/tip/hash").strip()
        if rest_height != self.best_height or rest_hash != self.best_hash:
            raise HarnessError(
                f"REST tip mismatch: electrs {rest_height}/{rest_hash}, "
                f"qbitd {self.best_height}/{self.best_hash}"
            )

        height_hash = http_text(self.http_port, f"/block-height/{self.best_height}").strip()
        if height_hash != self.best_hash:
            raise HarnessError(f"REST block-height mismatch: {height_hash} != {self.best_hash}")

        rest_header = http_text(self.http_port, f"/block/{self.best_hash}/header").strip()
        qbit_header = self.qbit_cli_call("getblockheader", self.best_hash, "false")
        if len(rest_header) != 160:
            raise HarnessError(f"REST header is not 80 bytes: {len(rest_header)} hex chars")
        if isinstance(qbit_header, str) and rest_header.lower() != qbit_header.lower():
            raise HarnessError("REST header does not match qbitd getblockheader hex")
        if block_hash_from_header_hex(rest_header) != self.best_hash:
            raise HarnessError("REST header hash does not match qbitd tip")

        stats = http_json(self.http_port, f"/address/{self.p2mr_address}")
        chain_stats = stats.get("chain_stats", {})
        if int(chain_stats.get("funded_txo_count", 0)) < 1:
            raise HarnessError(f"REST address stats did not index P2MR funding: {stats}")
        utxos = http_json(self.http_port, f"/address/{self.p2mr_address}/utxo")
        if not isinstance(utxos, list) or not utxos:
            raise HarnessError("REST address UTXO set is empty for mined P2MR address")

        mempool_txids = http_json(self.http_port, "/mempool/txids")
        if mempool_txids != []:
            raise HarnessError(f"expected empty mempool after mining-only smoke: {mempool_txids}")

    def check_electrum_parity(self) -> None:
        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")

            version = self.electrum_request(
                file,
                1,
                "server.version",
                ["qbit-electrs-harness", "1.4"],
            )
            if (
                not isinstance(version, list)
                or len(version) != 2
                or not str(version[0]).startswith("qbit-electrs ")
                or str(version[1]) != "1.4"
            ):
                raise HarnessError(f"unexpected Electrum server.version result: {version}")

            header = self.electrum_request(file, 2, "blockchain.headers.subscribe", [])
            if not isinstance(header, dict):
                raise HarnessError(f"unexpected Electrum header result: {header}")
            if int(header.get("height", -1)) != self.best_height:
                raise HarnessError(f"Electrum height mismatch: {header}")
            if len(str(header.get("hex", ""))) != 160:
                raise HarnessError(f"Electrum header is not 80 bytes: {header}")
            if block_hash_from_header_hex(str(header["hex"])) != self.best_hash:
                raise HarnessError(f"Electrum header hash does not match qbitd tip: {header}")

    def check_electrum_fee_methods(self) -> None:
        network_info = self.qbit_cli_call("getnetworkinfo")
        if not isinstance(network_info, dict):
            raise HarnessError(f"qbitd getnetworkinfo returned invalid result: {network_info}")
        expected_relayfee = float(network_info.get("relayfee", -1.0))
        if expected_relayfee <= 0:
            raise HarnessError(f"qbitd returned invalid relayfee: {network_info}")

        def require_fee(method: str, value: object) -> float:
            if not isinstance(value, (int, float)):
                raise HarnessError(f"Electrum {method} returned non-numeric fee: {value}")
            fee = float(value)
            if abs(fee - expected_relayfee) > 1e-12:
                raise HarnessError(
                    f"Electrum {method} fee {fee} differs from qbitd relayfee "
                    f"{expected_relayfee} BTC/kB"
                )
            return fee

        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")
            relayfee = self.electrum_request(file, 30, "blockchain.relayfee", [])
            self.electrum_relayfee_btc_kb = require_fee("blockchain.relayfee", relayfee)

            estimates: dict[str, float] = {}
            for request_id, target in enumerate(QBIT_ESTIMATEFEE_TARGETS, start=31):
                estimate = self.electrum_request(
                    file,
                    request_id,
                    "blockchain.estimatefee",
                    [target],
                )
                estimates[str(target)] = require_fee(
                    f"blockchain.estimatefee({target})",
                    estimate,
                )
            self.electrum_estimatefee_targets = estimates

        fee_estimates = http_json(self.http_port, "/fee-estimates")
        if not isinstance(fee_estimates, dict):
            raise HarnessError(f"REST /fee-estimates returned non-object: {fee_estimates}")

        expected_rest_fee = expected_relayfee * 100_000.0
        rest_estimates: dict[str, float] = {}
        expected_targets = {str(target) for target in QBIT_REST_FEE_ESTIMATE_TARGETS}
        if set(fee_estimates) != expected_targets:
            raise HarnessError(
                f"REST /fee-estimates target mismatch: {sorted(fee_estimates)} "
                f"!= {sorted(expected_targets)}"
            )
        for target in QBIT_REST_FEE_ESTIMATE_TARGETS:
            value = fee_estimates.get(str(target))
            if not isinstance(value, (int, float)):
                raise HarnessError(
                    f"REST /fee-estimates target {target} returned non-numeric fee: {value}"
                )
            fee = float(value)
            if abs(fee - expected_rest_fee) > 1e-8:
                raise HarnessError(
                    f"REST /fee-estimates target {target} fee {fee} differs from "
                    f"qbitd relayfee {expected_rest_fee} sat/vB"
                )
            rest_estimates[str(target)] = fee
        self.rest_fee_estimate_targets = rest_estimates

    def broadcast_raw_via_electrum(self, tx_hex: str) -> str:
        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")
            txid = self.electrum_request(
                file,
                3,
                "blockchain.transaction.broadcast",
                [tx_hex],
            )
        if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-f]{64}", txid):
            raise HarnessError(f"Electrum broadcast returned invalid txid: {txid}")
        return txid

    def create_p2mr_mempool_spend(self) -> None:
        self.spend_address = self.new_p2mr_address()
        self.spend_script_hash, self.spend_electrum_scripthash = self.p2mr_script_hashes(
            self.spend_address
        )
        spendable = self.qbit_cli_call(
            "listunspent",
            1,
            9999999,
            json.dumps([self.p2mr_address]),
            wallet=self.wallet_name,
        )
        if not isinstance(spendable, list) or len(spendable) != 1:
            raise HarnessError(f"expected one spendable probe P2MR UTXO: {spendable}")
        funding = spendable[0]
        funding_txid = funding.get("txid")
        funding_vout = funding.get("vout")
        if (
            not isinstance(funding_txid, str)
            or not re.fullmatch(r"[0-9a-f]{64}", funding_txid)
            or not isinstance(funding_vout, int)
            or funding.get("address") != self.p2mr_address
            or not funding.get("spendable")
        ):
            raise HarnessError(f"invalid probe P2MR funding UTXO: {funding}")

        change_address = self.qbit_cli_call(
            "getrawchangeaddress",
            "p2mr",
            wallet=self.wallet_name,
        )
        if not isinstance(change_address, str) or not change_address.startswith("qbrt1"):
            raise HarnessError(f"wallet returned non-regtest P2MR change address: {change_address}")
        self.p2mr_script_hashes(change_address)

        info(f"creating {SPEND_AMOUNT_BTC} qbit P2MR mempool spend from probe UTXO")
        inputs = [{"txid": funding_txid, "vout": funding_vout}]
        outputs = {self.spend_address: float(SPEND_AMOUNT_BTC)}
        options = {
            "add_inputs": False,
            "changeAddress": change_address,
            "fee_rate": 1,
        }
        funded_psbt = self.qbit_cli_call(
            "walletcreatefundedpsbt",
            json.dumps(inputs),
            json.dumps(outputs),
            0,
            json.dumps(options),
            wallet=self.wallet_name,
        )
        if not isinstance(funded_psbt, dict) or not isinstance(funded_psbt.get("psbt"), str):
            raise HarnessError(f"walletcreatefundedpsbt returned invalid result: {funded_psbt}")

        processed = self.qbit_cli_call(
            "walletprocesspsbt",
            funded_psbt["psbt"],
            "true",
            "ALL",
            "true",
            wallet=self.wallet_name,
        )
        if (
            not isinstance(processed, dict)
            or processed.get("complete") is not True
            or not isinstance(processed.get("hex"), str)
        ):
            raise HarnessError(f"walletprocesspsbt did not finalize the P2MR spend: {processed}")

        acceptance = self.qbit_cli_call("testmempoolaccept", json.dumps([processed["hex"]]))
        if (
            not isinstance(acceptance, list)
            or len(acceptance) != 1
            or acceptance[0].get("allowed") is not True
        ):
            raise HarnessError(f"qbitd rejected deterministic P2MR spend: {acceptance}")

        info("broadcasting deterministic P2MR spend through Electrum")
        txid = self.broadcast_raw_via_electrum(processed["hex"])
        if txid != acceptance[0].get("txid"):
            raise HarnessError(f"Electrum broadcast txid differs from qbitd preflight: {txid}")

        raw_hex = self.qbit_cli_call("getrawtransaction", txid, "false")
        raw_verbose = self.qbit_cli_call("getrawtransaction", txid, "true")
        if not isinstance(raw_hex, str) or not re.fullmatch(r"[0-9a-f]+", raw_hex):
            raise HarnessError(f"qbitd returned invalid raw spend hex: {raw_hex}")
        if not isinstance(raw_verbose, dict):
            raise HarnessError(f"qbitd returned invalid verbose spend tx: {raw_verbose}")

        vins = raw_verbose.get("vin")
        if not isinstance(vins, list) or len(vins) != 1:
            raise HarnessError(f"expected one-input wallet spend: {raw_verbose}")
        prev_txid = vins[0].get("txid")
        prev_vout = vins[0].get("vout")
        if prev_txid != funding_txid or prev_vout != funding_vout:
            raise HarnessError(f"wallet spend input does not match selected P2MR UTXO: {raw_verbose}")

        recipient_outputs = [
            output
            for output in raw_verbose.get("vout", [])
            if output.get("scriptPubKey", {}).get("address") == self.spend_address
        ]
        if len(recipient_outputs) != 1:
            raise HarnessError(
                f"expected one recipient P2MR output for {self.spend_address}: {raw_verbose}"
            )
        recipient_vout = recipient_outputs[0].get("n")
        if not isinstance(recipient_vout, int):
            raise HarnessError(f"recipient output did not include a vout index: {recipient_outputs}")
        if abs(float(recipient_outputs[0].get("value", 0.0)) - float(SPEND_AMOUNT_BTC)) > 1e-8:
            raise HarnessError(f"unexpected recipient output value: {recipient_outputs[0]}")

        mempool = self.qbit_cli_call("getrawmempool")
        if not isinstance(mempool, list) or txid not in mempool:
            raise HarnessError(f"qbitd mempool does not include wallet spend {txid}: {mempool}")
        mempool_entry = self.qbit_cli_call("getmempoolentry", txid)
        if not isinstance(mempool_entry, dict):
            raise HarnessError(f"qbitd getmempoolentry returned invalid result: {mempool_entry}")
        expected_size = len(bytes.fromhex(raw_hex))
        for field in ["vsize", "weight", "ancestorsize", "descendantsize"]:
            if int(mempool_entry.get(field, -1)) != expected_size:
                raise HarnessError(
                    f"qbitd mempool {field} should match WSF=1 tx bytes "
                    f"{expected_size}: {mempool_entry}"
                )
        fees = mempool_entry.get("fees", {})
        base_fee_sats = (
            round(float(fees.get("base", -1)) * 100_000_000)
            if isinstance(fees, dict)
            else -1
        )
        if base_fee_sats != expected_size:
            raise HarnessError(
                f"qbitd mempool base fee should equal 1 sat/vB over {expected_size} bytes: "
                f"{mempool_entry}"
            )

        self.spend_txid = txid
        self.spend_tx_hex = raw_hex
        self.spend_mempool_txids = mempool
        self.spend_mempool_entry = mempool_entry
        self.spend_prevout_txid = prev_txid
        self.spend_prevout_vout = prev_vout
        self.spend_recipient_vout = recipient_vout

    def wait_for_electrs_mempool_spend(self) -> None:
        deadline = time.monotonic() + self.args.timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.electrs_proc and self.electrs_proc.poll() is not None:
                raise HarnessError(
                    f"electrs exited before seeing mempool spend; see {self.electrs_log}\n"
                    + tail_file(self.electrs_log)
                )
            try:
                txids = http_json(self.http_port, "/mempool/txids")
                status = http_json(self.http_port, f"/tx/{self.spend_txid}/status")
                if self.spend_txid in txids and status.get("confirmed") is False:
                    return
                last_error = f"mempool={txids}, status={status}"
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                last_error = str(exc)
            time.sleep(1.0)
        raise HarnessError(f"timed out waiting for electrs mempool spend: {last_error}")

    def wait_for_electrs_confirmed_spend(self) -> None:
        deadline = time.monotonic() + self.args.timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.electrs_proc and self.electrs_proc.poll() is not None:
                raise HarnessError(
                    f"electrs exited before confirming spend; see {self.electrs_log}\n"
                    + tail_file(self.electrs_log)
                )
            try:
                txids = http_json(self.http_port, "/mempool/txids")
                status = http_json(self.http_port, f"/tx/{self.spend_txid}/status")
                if (
                    self.spend_txid not in txids
                    and status.get("confirmed") is True
                    and int(status.get("block_height", -1)) == self.spend_confirmed_height
                ):
                    return
                last_error = f"mempool={txids}, status={status}"
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                last_error = str(exc)
            time.sleep(1.0)
        raise HarnessError(f"timed out waiting for electrs confirmed spend: {last_error}")

    def check_rest_mempool_spend(self) -> None:
        txids = http_json(self.http_port, "/mempool/txids")
        if self.spend_txid not in txids:
            raise HarnessError(f"REST mempool txids missing spend {self.spend_txid}: {txids}")

        tx_hex = http_text(self.http_port, f"/tx/{self.spend_txid}/hex").strip()
        if tx_hex.lower() != self.spend_tx_hex.lower():
            raise HarnessError("REST tx hex does not match qbitd raw mempool tx")

        status = http_json(self.http_port, f"/tx/{self.spend_txid}/status")
        if status.get("confirmed") is not False:
            raise HarnessError(f"REST mempool spend should be unconfirmed: {status}")

        tx = http_json(self.http_port, f"/tx/{self.spend_txid}")
        self.check_rest_spend_tx_body(tx, expected_confirmed=False)

        stats = http_json(self.http_port, f"/address/{self.spend_address}")
        chain_stats = stats.get("chain_stats", {})
        mempool_stats = stats.get("mempool_stats", {})
        if (
            int(chain_stats.get("tx_count", 0)) != 0
            or int(chain_stats.get("funded_txo_count", 0)) != 0
            or int(chain_stats.get("funded_txo_sum", 0)) != 0
            or int(chain_stats.get("spent_txo_count", 0)) != 0
            or int(chain_stats.get("spent_txo_sum", 0)) != 0
        ):
            raise HarnessError(f"REST address chain stats should be empty for mempool spend: {stats}")
        if (
            int(mempool_stats.get("funded_txo_count", 0)) < 1
            or int(mempool_stats.get("funded_txo_sum", 0)) < SPEND_AMOUNT_SATS
        ):
            raise HarnessError(f"REST address mempool stats missing P2MR spend: {stats}")

        mempool_txs = http_json(
            self.http_port,
            f"/address/{self.spend_address}/txs/mempool?max_txs=5",
        )
        if not any(tx.get("txid") == self.spend_txid for tx in mempool_txs):
            raise HarnessError(f"REST address mempool txs missing spend: {mempool_txs}")

        self.check_rest_address_scripthash_parity(expected_confirmed=False)
        self.check_rest_prevout_spend(expected_confirmed=False)

    def check_mempool_aggregate_surfaces(self, phase: str) -> None:
        expected_size = self.expected_spend_vsize()
        expected_fee = self.expected_spend_fee_sats()
        expected_fee_rate = expected_fee / expected_size

        backlog = self.wait_for_rest_mempool_backlog(
            expected_count=1,
            expected_size=expected_size,
            expected_fee=expected_fee,
        )
        self.check_fee_histogram(
            "REST /mempool fee_histogram",
            backlog.get("fee_histogram"),
            expected_fee_rate,
            expected_size,
        )

        recent = http_json(self.http_port, "/mempool/recent")
        if not isinstance(recent, list):
            raise HarnessError(f"REST /mempool/recent returned non-list: {recent}")
        matching = [
            entry
            for entry in recent
            if isinstance(entry, dict) and entry.get("txid") == self.spend_txid
        ]
        if not matching:
            raise HarnessError(f"REST /mempool/recent missing P2MR spend: {recent}")
        if len(matching) <= self.rest_recent_spend_matches:
            raise HarnessError(
                "REST /mempool/recent did not record a fresh P2MR spend entry for "
                f"{phase}: previous matches={self.rest_recent_spend_matches}, "
                f"current matches={len(matching)}, recent={recent}"
            )
        self.rest_recent_spend_matches = len(matching)
        recent_entry = matching[0]
        if (
            int(recent_entry.get("fee", -1)) != expected_fee
            or int(recent_entry.get("vsize", -1)) != expected_size
        ):
            raise HarnessError(
                "REST /mempool/recent should expose qbit byte-vsize and fee for "
                f"the P2MR spend: {recent_entry}"
            )

        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")
            histogram = self.electrum_request(file, 15, "mempool.get_fee_histogram", [])
        self.check_fee_histogram(
            "Electrum mempool.get_fee_histogram",
            histogram,
            expected_fee_rate,
            expected_size,
        )
        self.mempool_aggregate_surfaces[phase] = {
            "state": "mempool",
            "expected_fee_sats": expected_fee,
            "expected_vsize": expected_size,
            "rest_backlog": backlog,
            "rest_recent_entry": recent_entry,
            "rest_recent_matching_entries": len(matching),
            "electrum_fee_histogram": histogram,
        }

    def check_mempool_aggregate_empty(self, phase: str) -> None:
        backlog = self.wait_for_rest_mempool_backlog(
            expected_count=0,
            expected_size=0,
            expected_fee=0,
        )
        self.check_empty_fee_histogram("REST /mempool fee_histogram", backlog.get("fee_histogram"))

        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")
            histogram = self.electrum_request(file, 15, "mempool.get_fee_histogram", [])
        self.check_empty_fee_histogram("Electrum mempool.get_fee_histogram", histogram)

        self.mempool_aggregate_surfaces[phase] = {
            "state": "empty",
            "rest_backlog": backlog,
            "electrum_fee_histogram": histogram,
        }

    def wait_for_rest_mempool_backlog(
        self,
        expected_count: int,
        expected_size: int,
        expected_fee: int,
    ) -> dict:
        deadline = time.monotonic() + self.args.timeout
        last_backlog: object = None
        while time.monotonic() < deadline:
            backlog = http_json(self.http_port, "/mempool")
            last_backlog = backlog
            if not isinstance(backlog, dict):
                raise HarnessError(f"REST /mempool returned non-object: {backlog}")
            if (
                int(backlog.get("count", -1)) == expected_count
                and int(backlog.get("vsize", -1)) == expected_size
                and int(backlog.get("total_fee", -1)) == expected_fee
            ):
                return backlog
            time.sleep(0.5)
        raise HarnessError(
            "REST /mempool did not converge to expected qbit byte-vsize/fee "
            f"totals: count={expected_count}, vsize={expected_size}, "
            f"fee={expected_fee}, last={last_backlog}"
        )

    def expected_spend_vsize(self) -> int:
        if not self.spend_tx_hex:
            raise HarnessError("P2MR spend hex was not captured")
        return len(bytes.fromhex(self.spend_tx_hex))

    def expected_spend_fee_sats(self) -> int:
        if self.spend_mempool_entry is None:
            raise HarnessError("P2MR spend mempool entry was not captured")
        fees = self.spend_mempool_entry.get("fees", {})
        if not isinstance(fees, dict):
            raise HarnessError(f"qbitd mempool entry missing fees: {self.spend_mempool_entry}")
        fee = round(float(fees.get("base", -1)) * 100_000_000)
        if fee <= 0:
            raise HarnessError(f"qbitd mempool entry has invalid base fee: {fees}")
        return fee

    @staticmethod
    def check_fee_histogram(
        label: str,
        histogram: object,
        expected_fee_rate: float,
        expected_vsize: int,
    ) -> None:
        if not isinstance(histogram, list) or len(histogram) != 1:
            raise HarnessError(f"{label} should contain one qbit P2MR bin: {histogram}")
        bin_value = histogram[0]
        if not isinstance(bin_value, list) or len(bin_value) != 2:
            raise HarnessError(f"{label} bin has unexpected shape: {histogram}")
        fee_rate, vsize = bin_value
        if not isinstance(fee_rate, (int, float)) or not isinstance(vsize, int):
            raise HarnessError(f"{label} bin has non-numeric values: {histogram}")
        if abs(float(fee_rate) - expected_fee_rate) > 1e-8 or vsize != expected_vsize:
            raise HarnessError(
                f"{label} should use qbit byte-vsize {expected_vsize} at fee rate "
                f"{expected_fee_rate}: {histogram}"
            )

    @staticmethod
    def check_empty_fee_histogram(label: str, histogram: object) -> None:
        if histogram != []:
            raise HarnessError(f"{label} should be empty after confirmation: {histogram}")

    def check_rest_confirmed_spend(self) -> None:
        txids = http_json(self.http_port, "/mempool/txids")
        if self.spend_txid in txids:
            raise HarnessError(f"REST mempool still includes confirmed spend: {txids}")

        status = http_json(self.http_port, f"/tx/{self.spend_txid}/status")
        if (
            status.get("confirmed") is not True
            or int(status.get("block_height", -1)) != self.spend_confirmed_height
        ):
            raise HarnessError(f"REST spend confirmation status mismatch: {status}")

        tx = http_json(self.http_port, f"/tx/{self.spend_txid}")
        self.check_rest_spend_tx_body(tx, expected_confirmed=True)
        self.check_rest_confirmed_block()

        stats = http_json(self.http_port, f"/address/{self.spend_address}")
        chain_stats = stats.get("chain_stats", {})
        mempool_stats = stats.get("mempool_stats", {})
        if (
            int(chain_stats.get("funded_txo_count", 0)) < 1
            or int(chain_stats.get("funded_txo_sum", 0)) < SPEND_AMOUNT_SATS
            or int(mempool_stats.get("tx_count", 0)) != 0
        ):
            raise HarnessError(f"REST address chain stats missing confirmed P2MR spend: {stats}")

        utxos = http_json(self.http_port, f"/address/{self.spend_address}/utxo")
        if not any(
            utxo.get("txid") == self.spend_txid
            and int(utxo.get("vout", -1)) == self.spend_recipient_vout
            and int(utxo.get("value", 0)) == SPEND_AMOUNT_SATS
            for utxo in utxos
        ):
            raise HarnessError(f"REST address UTXOs missing confirmed P2MR spend: {utxos}")

        self.check_rest_address_scripthash_parity(expected_confirmed=True)
        self.check_rest_prevout_spend(expected_confirmed=True)

    def check_rest_spend_tx_body(self, tx: dict, expected_confirmed: bool) -> None:
        if (
            tx.get("txid") != self.spend_txid
            or tx.get("status", {}).get("confirmed") is not expected_confirmed
        ):
            raise HarnessError(f"REST tx body status mismatch for P2MR spend: {tx}")
        expected_size = len(bytes.fromhex(self.spend_tx_hex))
        if (
            int(tx.get("size", -1)) != expected_size
            or int(tx.get("weight", -1)) != expected_size
            or int(tx.get("vsize", -1)) != expected_size
        ):
            raise HarnessError(f"REST qbit WSF=1 size fields mismatch: {tx}")

        vins = tx.get("vin")
        if (
            not isinstance(vins, list)
            or len(vins) != 1
            or vins[0].get("txid") != self.spend_prevout_txid
            or int(vins[0].get("vout", -1)) != self.spend_prevout_vout
        ):
            raise HarnessError(f"REST tx input mismatch for P2MR spend: {tx}")
        witness = vins[0].get("witness")
        if not isinstance(witness, list) or not witness:
            raise HarnessError(f"REST tx input missing P2MR witness stack: {tx}")
        inner_witness_script = vins[0].get("inner_witnessscript_asm")
        if not isinstance(inner_witness_script, str) or not inner_witness_script:
            raise HarnessError(f"REST tx input missing P2MR inner witness script: {tx}")
        prevout = vins[0].get("prevout")
        if (
            not isinstance(prevout, dict)
            or prevout.get("scriptpubkey_type") != "v2_p2mr"
            or prevout.get("scriptpubkey_address") != self.p2mr_address
        ):
            raise HarnessError(f"REST tx input prevout mismatch for P2MR spend: {tx}")

        vouts = tx.get("vout")
        if not isinstance(vouts, list) or self.spend_recipient_vout >= len(vouts):
            raise HarnessError(f"REST tx outputs missing P2MR recipient vout: {tx}")
        recipient = vouts[self.spend_recipient_vout]
        if (
            recipient.get("scriptpubkey_type") != "v2_p2mr"
            or recipient.get("scriptpubkey_address") != self.spend_address
            or int(recipient.get("value", 0)) != SPEND_AMOUNT_SATS
        ):
            raise HarnessError(f"REST tx output mismatch for P2MR recipient: {recipient}")

    def check_rest_confirmed_block(self) -> None:
        if not self.spend_block_hex or not self.spend_block_json or not self.spend_block_txids:
            raise HarnessError("confirmed spend block RPC truth was not captured")

        block = http_json(self.http_port, f"/block/{self.best_hash}")
        expected_size = len(bytes.fromhex(self.spend_block_hex))
        expected_tx_count = int(self.spend_block_json.get("nTx", len(self.spend_block_txids)))
        if (
            block.get("id") != self.best_hash
            or int(block.get("height", -1)) != self.spend_confirmed_height
            or int(block.get("tx_count", -1)) != expected_tx_count
            or int(block.get("size", -1)) != expected_size
            or int(block.get("weight", -1)) != expected_size
            or block.get("merkle_root") != self.spend_block_json.get("merkleroot")
        ):
            raise HarnessError(f"REST block metadata mismatch for P2MR spend block: {block}")

        raw_block = http_bytes(self.http_port, f"/block/{self.best_hash}/raw")
        if raw_block.hex() != self.spend_block_hex.lower():
            raise HarnessError("REST raw block bytes do not match qbitd spend block")

        rest_txids = http_json(self.http_port, f"/block/{self.best_hash}/txids")
        if rest_txids != self.spend_block_txids:
            raise HarnessError(f"REST block txids mismatch: {rest_txids} != {self.spend_block_txids}")

        indexed_txid = http_text(
            self.http_port,
            f"/block/{self.best_hash}/txid/{self.spend_tx_block_pos}",
        ).strip()
        if indexed_txid != self.spend_txid:
            raise HarnessError(f"REST block txid index mismatch: {indexed_txid}")

        block_txs = http_json(self.http_port, f"/block/{self.best_hash}/txs")
        spend_txs = [tx for tx in block_txs if tx.get("txid") == self.spend_txid]
        if len(spend_txs) != 1:
            raise HarnessError(f"REST block tx page missing P2MR spend: {block_txs}")
        self.check_rest_spend_tx_body(spend_txs[0], expected_confirmed=True)

        merkle_proof = http_json(self.http_port, f"/tx/{self.spend_txid}/merkle-proof")
        if (
            int(merkle_proof.get("block_height", -1)) != self.spend_confirmed_height
            or int(merkle_proof.get("pos", -1)) != self.spend_tx_block_pos
            or not isinstance(merkle_proof.get("merkle"), list)
        ):
            raise HarnessError(f"REST merkle proof mismatch for P2MR spend: {merkle_proof}")

        merkleblock = http_text(self.http_port, f"/tx/{self.spend_txid}/merkleblock-proof").strip()
        header = http_text(self.http_port, f"/block/{self.best_hash}/header").strip()
        if not re.fullmatch(r"[0-9a-f]+", merkleblock) or not merkleblock.startswith(header):
            raise HarnessError("REST merkleblock proof does not start with the pure qbit header")

    def check_rest_address_scripthash_parity(self, expected_confirmed: bool) -> None:
        address_base = f"/address/{self.spend_address}"
        scripthash_base = f"/scripthash/{self.spend_script_hash}"
        address_stats = http_json(self.http_port, address_base)
        scripthash_stats = http_json(self.http_port, scripthash_base)
        if address_stats.get("chain_stats") != scripthash_stats.get("chain_stats"):
            raise HarnessError(
                f"REST address/scripthash chain stats mismatch: {address_stats} != {scripthash_stats}"
            )
        if address_stats.get("mempool_stats") != scripthash_stats.get("mempool_stats"):
            raise HarnessError(
                f"REST address/scripthash mempool stats mismatch: {address_stats} != {scripthash_stats}"
            )

        address_utxos = http_json(self.http_port, f"{address_base}/utxo")
        scripthash_utxos = http_json(self.http_port, f"{scripthash_base}/utxo")
        if canonical_json(address_utxos) != canonical_json(scripthash_utxos):
            raise HarnessError(
                f"REST address/scripthash UTXO mismatch: {address_utxos} != {scripthash_utxos}"
            )

        combined_path = "txs?max_txs=5"
        address_txs = http_json(self.http_port, f"{address_base}/{combined_path}")
        scripthash_txs = http_json(self.http_port, f"{scripthash_base}/{combined_path}")
        self.check_matching_rest_tx_lists(address_txs, scripthash_txs, "combined history")

        if expected_confirmed:
            chain_path = "txs/chain?max_txs=5"
            address_chain_txs = http_json(self.http_port, f"{address_base}/{chain_path}")
            scripthash_chain_txs = http_json(self.http_port, f"{scripthash_base}/{chain_path}")
            self.check_matching_rest_tx_lists(
                address_chain_txs,
                scripthash_chain_txs,
                "chain history",
            )
            summary_path = "txs/summary?max_txs=5"
            address_summary = http_json(self.http_port, f"{address_base}/{summary_path}")
            scripthash_summary = http_json(self.http_port, f"{scripthash_base}/{summary_path}")
            if canonical_json(address_summary) != canonical_json(scripthash_summary):
                raise HarnessError(
                    "REST address/scripthash summary mismatch: "
                    f"{address_summary} != {scripthash_summary}"
                )
        else:
            mempool_path = "txs/mempool?max_txs=5"
            address_mempool_txs = http_json(self.http_port, f"{address_base}/{mempool_path}")
            scripthash_mempool_txs = http_json(self.http_port, f"{scripthash_base}/{mempool_path}")
            self.check_matching_rest_tx_lists(
                address_mempool_txs,
                scripthash_mempool_txs,
                "mempool history",
            )

    def check_matching_rest_tx_lists(self, left: object, right: object, label: str) -> None:
        if not isinstance(left, list) or not isinstance(right, list):
            raise HarnessError(f"REST {label} should return lists: {left} / {right}")
        left_txids = [tx.get("txid") for tx in left if isinstance(tx, dict)]
        right_txids = [tx.get("txid") for tx in right if isinstance(tx, dict)]
        if left_txids != right_txids:
            raise HarnessError(f"REST address/scripthash {label} txids mismatch: {left} != {right}")
        if self.spend_txid not in left_txids:
            raise HarnessError(f"REST {label} missing P2MR spend {self.spend_txid}: {left}")

    def check_rest_prevout_spend(self, expected_confirmed: bool) -> None:
        outspend = http_json(
            self.http_port,
            f"/tx/{self.spend_prevout_txid}/outspend/{self.spend_prevout_vout}",
        )
        if (
            outspend.get("spent") is not True
            or outspend.get("txid") != self.spend_txid
            or int(outspend.get("vin", -1)) != 0
            or outspend.get("status", {}).get("confirmed") is not expected_confirmed
        ):
            raise HarnessError(f"REST prevout spend mismatch: {outspend}")

        outspends = http_json(self.http_port, f"/tx/{self.spend_prevout_txid}/outspends")
        if self.spend_prevout_vout >= len(outspends):
            raise HarnessError(f"REST outspends too short for prevout: {outspends}")
        indexed = outspends[self.spend_prevout_vout]
        if (
            indexed.get("spent") is not True
            or indexed.get("txid") != self.spend_txid
            or int(indexed.get("vin", -1)) != 0
            or indexed.get("status", {}).get("confirmed") is not expected_confirmed
        ):
            raise HarnessError(f"REST indexed prevout spend mismatch: {outspends}")

    def check_electrum_mempool_spend(self) -> None:
        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")
            tx_hex = self.electrum_request(
                file,
                10,
                "blockchain.transaction.get",
                [self.spend_txid],
            )
            if tx_hex != self.spend_tx_hex:
                raise HarnessError("Electrum transaction.get does not match qbitd mempool tx")

            subscription_status = self.electrum_request(
                file,
                14,
                "blockchain.scripthash.subscribe",
                [self.spend_electrum_scripthash],
            )
            if not isinstance(subscription_status, str) or not subscription_status:
                raise HarnessError(
                    f"Electrum mempool subscribe missing P2MR status: {subscription_status}"
                )

            history = self.electrum_request(
                file,
                11,
                "blockchain.scripthash.get_history",
                [self.spend_electrum_scripthash],
            )
            if not any(
                item.get("tx_hash") == self.spend_txid and int(item.get("height", -999)) == 0
                for item in history
            ):
                raise HarnessError(f"Electrum mempool history missing P2MR spend: {history}")

            balance = self.electrum_request(
                file,
                12,
                "blockchain.scripthash.get_balance",
                [self.spend_electrum_scripthash],
            )
            if (
                int(balance.get("confirmed", -1)) != 0
                or int(balance.get("unconfirmed", -1)) != SPEND_AMOUNT_SATS
            ):
                raise HarnessError(f"Electrum mempool balance mismatch: {balance}")

            utxos = self.electrum_request(
                file,
                13,
                "blockchain.scripthash.listunspent",
                [self.spend_electrum_scripthash],
            )
            if not any(
                utxo.get("tx_hash") == self.spend_txid
                and int(utxo.get("tx_pos", -1)) == self.spend_recipient_vout
                and int(utxo.get("height", -1)) == 0
                and int(utxo.get("value", 0)) == SPEND_AMOUNT_SATS
                for utxo in utxos
            ):
                raise HarnessError(f"Electrum mempool listunspent missing spend: {utxos}")
            self.check_rest_electrum_scripthash_parity(
                balance,
                utxos,
                expected_confirmed=False,
            )

    def check_electrum_confirmed_spend(self) -> None:
        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")
            tx_hex = self.electrum_request(
                file,
                20,
                "blockchain.transaction.get",
                [self.spend_txid],
            )
            if tx_hex != self.spend_tx_hex:
                raise HarnessError("Electrum transaction.get does not match confirmed qbitd tx")

            subscription_status = self.electrum_request(
                file,
                24,
                "blockchain.scripthash.subscribe",
                [self.spend_electrum_scripthash],
            )
            if not isinstance(subscription_status, str) or not subscription_status:
                raise HarnessError(
                    f"Electrum confirmed subscribe missing P2MR status: {subscription_status}"
                )

            history = self.electrum_request(
                file,
                21,
                "blockchain.scripthash.get_history",
                [self.spend_electrum_scripthash],
            )
            if not any(
                item.get("tx_hash") == self.spend_txid
                and int(item.get("height", -1)) == self.spend_confirmed_height
                for item in history
            ):
                raise HarnessError(f"Electrum confirmed history missing P2MR spend: {history}")

            balance = self.electrum_request(
                file,
                22,
                "blockchain.scripthash.get_balance",
                [self.spend_electrum_scripthash],
            )
            if (
                int(balance.get("confirmed", -1)) != SPEND_AMOUNT_SATS
                or int(balance.get("unconfirmed", -1)) != 0
            ):
                raise HarnessError(f"Electrum confirmed balance mismatch: {balance}")

            utxos = self.electrum_request(
                file,
                23,
                "blockchain.scripthash.listunspent",
                [self.spend_electrum_scripthash],
            )
            if not any(
                utxo.get("tx_hash") == self.spend_txid
                and int(utxo.get("tx_pos", -1)) == self.spend_recipient_vout
                and int(utxo.get("height", -1)) == self.spend_confirmed_height
                and int(utxo.get("value", 0)) == SPEND_AMOUNT_SATS
                for utxo in utxos
            ):
                raise HarnessError(f"Electrum confirmed listunspent missing spend: {utxos}")
            self.check_rest_electrum_scripthash_parity(
                balance,
                utxos,
                expected_confirmed=True,
            )

    def check_rest_electrum_scripthash_parity(
        self,
        electrum_balance: dict,
        electrum_utxos: list,
        expected_confirmed: bool,
    ) -> None:
        stats = http_json(self.http_port, f"/scripthash/{self.spend_script_hash}")
        chain_stats = stats.get("chain_stats", {})
        mempool_stats = stats.get("mempool_stats", {})
        if expected_confirmed:
            rest_confirmed = int(chain_stats.get("funded_txo_sum", -1)) - int(
                chain_stats.get("spent_txo_sum", 0)
            )
            rest_unconfirmed = int(mempool_stats.get("funded_txo_sum", 0)) - int(
                mempool_stats.get("spent_txo_sum", 0)
            )
        else:
            rest_confirmed = int(chain_stats.get("funded_txo_sum", 0)) - int(
                chain_stats.get("spent_txo_sum", 0)
            )
            rest_unconfirmed = int(mempool_stats.get("funded_txo_sum", -1)) - int(
                mempool_stats.get("spent_txo_sum", 0)
            )
        if int(electrum_balance.get("confirmed", -1)) != rest_confirmed or int(
            electrum_balance.get("unconfirmed", -1)
        ) != rest_unconfirmed:
            raise HarnessError(
                f"REST/Electrum balance mismatch: rest={stats}, electrum={electrum_balance}"
            )

        rest_utxos = http_json(self.http_port, f"/scripthash/{self.spend_script_hash}/utxo")
        rest_outpoints = {
            (utxo.get("txid"), int(utxo.get("vout", -1)), int(utxo.get("value", 0)))
            for utxo in rest_utxos
        }
        electrum_outpoints = {
            (utxo.get("tx_hash"), int(utxo.get("tx_pos", -1)), int(utxo.get("value", 0)))
            for utxo in electrum_utxos
        }
        if rest_outpoints != electrum_outpoints:
            raise HarnessError(
                f"REST/Electrum UTXO mismatch: rest={rest_utxos}, electrum={electrum_utxos}"
            )

    def mine_mempool_spend(self) -> None:
        mine_address = self.new_p2mr_address()
        info("mining one qbit regtest block to confirm the P2MR spend")
        mined = self.qbit_cli_call("generatetoaddress", 1, mine_address)
        if not isinstance(mined, list) or len(mined) != 1:
            raise HarnessError(f"unexpected generatetoaddress result: {mined}")

        mempool = self.qbit_cli_call("getrawmempool")
        if isinstance(mempool, list) and self.spend_txid in mempool:
            raise HarnessError(f"qbitd mempool still includes mined spend: {mempool}")

        chain = self.qbit_cli_call("getblockchaininfo")
        if not isinstance(chain, dict):
            raise HarnessError(f"unexpected getblockchaininfo result after spend mining: {chain}")
        self.best_height = int(chain["blocks"])
        self.best_hash = str(chain["bestblockhash"])
        self.spend_confirmed_height = self.best_height
        self.capture_confirmed_spend_rpc()

    def capture_confirmed_spend_rpc(self) -> None:
        raw_block = self.qbit_cli_call("getblock", self.best_hash, "0")
        if not isinstance(raw_block, str) or not re.fullmatch(r"[0-9a-f]+", raw_block):
            raise HarnessError(f"qbitd returned invalid raw spend block hex: {raw_block}")
        block_json = self.qbit_cli_call("getblock", self.best_hash, "1")
        if not isinstance(block_json, dict):
            raise HarnessError(f"qbitd returned invalid spend block JSON: {block_json}")
        header_json = self.qbit_cli_call("getblockheader", self.best_hash, "true")
        if not isinstance(header_json, dict):
            raise HarnessError(f"qbitd returned invalid spend block header JSON: {header_json}")
        tx_json = self.qbit_cli_call("getrawtransaction", self.spend_txid, "true", self.best_hash)
        if not isinstance(tx_json, dict):
            raise HarnessError(f"qbitd returned invalid confirmed spend tx JSON: {tx_json}")
        txids = block_json.get("tx")
        if not isinstance(txids, list) or self.spend_txid not in txids:
            raise HarnessError(f"qbitd spend block does not include P2MR spend tx: {block_json}")
        self.spend_block_hex = raw_block
        self.spend_block_json = block_json
        self.spend_block_header_json = header_json
        self.spend_block_txids = [str(txid) for txid in txids]
        self.spend_tx_block_pos = self.spend_block_txids.index(self.spend_txid)
        self.spend_tx_confirmed_verbose = tx_json

    def invalidate_confirmed_spend_block(self) -> None:
        self.reorged_block_hash = self.best_hash
        info(f"invalidating qbit regtest block {self.reorged_block_hash} for P2MR reorg check")
        self.qbit_cli_call("invalidateblock", self.reorged_block_hash)
        chain = self.qbit_cli_call("getblockchaininfo")
        if not isinstance(chain, dict):
            raise HarnessError(f"unexpected getblockchaininfo result after invalidate: {chain}")
        self.best_height = int(chain["blocks"])
        self.best_hash = str(chain["bestblockhash"])
        self.reorged_to_height = self.best_height
        self.reorged_to_hash = self.best_hash

        mempool = self.qbit_cli_call("getrawmempool")
        if not isinstance(mempool, list) or self.spend_txid not in mempool:
            raise HarnessError(f"qbitd did not return invalidated P2MR spend to mempool: {mempool}")

    def reconsider_confirmed_spend_block(self) -> None:
        info(f"reconsidering qbit regtest block {self.reorged_block_hash}")
        self.qbit_cli_call("reconsiderblock", self.reorged_block_hash)
        chain = self.qbit_cli_call("getblockchaininfo")
        if not isinstance(chain, dict):
            raise HarnessError(f"unexpected getblockchaininfo result after reconsider: {chain}")
        self.best_height = int(chain["blocks"])
        self.best_hash = str(chain["bestblockhash"])
        self.spend_confirmed_height = self.best_height

        if self.best_hash != self.reorged_block_hash:
            raise HarnessError(
                f"qbitd did not restore reconsidered spend block: {self.best_hash} "
                f"!= {self.reorged_block_hash}"
            )
        mempool = self.qbit_cli_call("getrawmempool")
        if isinstance(mempool, list) and self.spend_txid in mempool:
            raise HarnessError(f"qbitd mempool still includes restored P2MR spend: {mempool}")

    def make_auxpow_payload_hex(self, template: dict, parent_time: int) -> str:
        try:
            from qbit_auxpow import QBIT_AUXPOW_CHAIN_ID, make_valid_auxpow_from_template
        except ImportError as exc:
            raise HarnessError(f"could not import local qbit AuxPoW helper: {exc}") from exc
        return make_valid_auxpow_from_template(
            template,
            parent_time=parent_time,
            expected_chain_id=QBIT_AUXPOW_CHAIN_ID,
        )

    def submit_auxpow_block(self) -> None:
        last_header = self.qbit_cli_call("getblockheader", self.best_hash, "true")
        if not isinstance(last_header, dict):
            raise HarnessError(f"qbitd returned invalid current header before AuxPoW: {last_header}")
        parent_time = int(last_header.get("time", int(time.time()))) + 600
        payout_address = self.new_p2mr_address()

        info("creating and submitting one qbit regtest AuxPoW block")
        self.qbit_cli_call("setmocktime", parent_time)
        try:
            template = self.qbit_cli_call("createauxblock", payout_address)
            if (
                not isinstance(template, dict)
                or not isinstance(template.get("hash"), str)
                or not isinstance(template.get("chainid"), int)
            ):
                raise HarnessError(f"qbitd returned invalid AuxPoW template: {template}")
            auxpow_hex = self.make_auxpow_payload_hex(template, parent_time)
            result = self.qbit_cli_call("submitauxblock", template["hash"], auxpow_hex)
            if result not in (None, ""):
                raise HarnessError(f"qbitd rejected AuxPoW block: {result}")
        finally:
            self.qbit_cli_call("setmocktime", 0)

        chain = self.qbit_cli_call("getblockchaininfo")
        if not isinstance(chain, dict):
            raise HarnessError(f"unexpected getblockchaininfo result after AuxPoW: {chain}")
        self.best_height = int(chain["blocks"])
        self.best_hash = str(chain["bestblockhash"])
        if self.best_hash != template["hash"]:
            raise HarnessError(f"qbitd AuxPoW tip mismatch: {self.best_hash} != {template['hash']}")

        raw_block = self.qbit_cli_call("getblock", self.best_hash, "0")
        block_json = self.qbit_cli_call("getblock", self.best_hash, "1")
        extended_header = self.qbit_cli_call("getblockheader", self.best_hash, "false")
        if not isinstance(raw_block, str) or not re.fullmatch(r"[0-9a-f]+", raw_block):
            raise HarnessError(f"qbitd returned invalid AuxPoW raw block: {raw_block}")
        if not isinstance(block_json, dict):
            raise HarnessError(f"qbitd returned invalid AuxPoW block JSON: {block_json}")
        if not isinstance(extended_header, str) or not re.fullmatch(r"[0-9a-f]+", extended_header):
            raise HarnessError(f"qbitd returned invalid AuxPoW extended header: {extended_header}")

        pure_header = raw_block[:PURE_BLOCK_HEADER_HEX_LEN]
        if (
            len(pure_header) != PURE_BLOCK_HEADER_HEX_LEN
            or not raw_block.startswith(extended_header)
            or not extended_header.startswith(pure_header)
            or len(extended_header) <= PURE_BLOCK_HEADER_HEX_LEN
        ):
            raise HarnessError("qbitd AuxPoW block/header boundaries are inconsistent")

        txids = block_json.get("tx")
        if not isinstance(txids, list) or not txids:
            raise HarnessError(f"qbitd AuxPoW block missing txids: {block_json}")
        version = int(block_json.get("version", 0))
        if version & 0x100 == 0:
            raise HarnessError(f"qbitd AuxPoW block does not signal AuxPoW: {block_json}")

        self.auxpow_block_hash = self.best_hash
        self.auxpow_block_height = self.best_height
        self.auxpow_block_hex = raw_block
        self.auxpow_block_json = block_json
        self.auxpow_extended_header_hex = extended_header
        self.auxpow_pure_header_hex = pure_header
        self.auxpow_txids = [str(txid) for txid in txids]

    def check_rest_auxpow_block(self) -> None:
        if (
            not self.auxpow_block_hash
            or not self.auxpow_block_hex
            or not self.auxpow_block_json
            or not self.auxpow_txids
        ):
            raise HarnessError("AuxPoW block truth was not captured")

        if self.best_hash != self.auxpow_block_hash or self.best_height != self.auxpow_block_height:
            raise HarnessError(
                f"electrs tip did not reach AuxPoW block: {self.best_height}/{self.best_hash}"
            )

        rest_header = http_text(self.http_port, f"/block/{self.auxpow_block_hash}/header").strip()
        if rest_header.lower() != self.auxpow_pure_header_hex.lower():
            raise HarnessError("REST AuxPoW block header does not match qbitd pure header")
        if rest_header.lower() == self.auxpow_extended_header_hex.lower():
            raise HarnessError("REST AuxPoW block header exposed qbitd extended header")

        raw_block = http_bytes(self.http_port, f"/block/{self.auxpow_block_hash}/raw")
        if raw_block.hex() != self.auxpow_block_hex.lower():
            raise HarnessError("REST AuxPoW raw block bytes do not match qbitd")

        block = http_json(self.http_port, f"/block/{self.auxpow_block_hash}")
        expected_size = len(bytes.fromhex(self.auxpow_block_hex))
        if (
            block.get("id") != self.auxpow_block_hash
            or int(block.get("height", -1)) != self.auxpow_block_height
            or int(block.get("tx_count", -1)) != len(self.auxpow_txids)
            or int(block.get("size", -1)) != expected_size
            or int(block.get("weight", -1)) != expected_size
            or block.get("merkle_root") != self.auxpow_block_json.get("merkleroot")
        ):
            raise HarnessError(f"REST AuxPoW block metadata mismatch: {block}")

        rest_txids = http_json(self.http_port, f"/block/{self.auxpow_block_hash}/txids")
        if rest_txids != self.auxpow_txids:
            raise HarnessError(f"REST AuxPoW txids mismatch: {rest_txids} != {self.auxpow_txids}")

        for index, txid in enumerate(self.auxpow_txids):
            indexed_txid = http_text(
                self.http_port,
                f"/block/{self.auxpow_block_hash}/txid/{index}",
            ).strip()
            if indexed_txid != txid:
                raise HarnessError(f"REST AuxPoW txid index {index} mismatch: {indexed_txid}")

        block_txs = http_json(self.http_port, f"/block/{self.auxpow_block_hash}/txs")
        page_txids = [tx.get("txid") for tx in block_txs]
        if page_txids != self.auxpow_txids[: len(page_txids)]:
            raise HarnessError(f"REST AuxPoW tx page mismatch: {block_txs}")

    def check_electrum_auxpow_block_headers(self) -> None:
        if (
            not self.auxpow_block_hash
            or not self.auxpow_pure_header_hex
            or not self.auxpow_extended_header_hex
        ):
            raise HarnessError("AuxPoW block header truth was not captured")

        height = self.auxpow_block_height
        if self.best_hash != self.auxpow_block_hash or self.best_height != height:
            raise HarnessError(
                f"electrs tip did not reach AuxPoW block for Electrum checks: "
                f"{self.best_height}/{self.best_hash}"
            )

        def require_pure_header(method: str, header_hex: object) -> str:
            if not isinstance(header_hex, str) or not re.fullmatch(r"[0-9a-f]+", header_hex):
                raise HarnessError(f"Electrum {method} returned invalid header hex: {header_hex}")
            if header_hex.lower() != self.auxpow_pure_header_hex.lower():
                raise HarnessError(f"Electrum {method} did not return the AuxPoW pure header")
            if header_hex.lower() == self.auxpow_extended_header_hex.lower():
                raise HarnessError(f"Electrum {method} exposed qbitd's AuxPoW-extended header")
            if len(header_hex) != PURE_BLOCK_HEADER_HEX_LEN:
                raise HarnessError(f"Electrum {method} header is not 80 bytes: {header_hex}")
            if block_hash_from_header_hex(header_hex) != self.auxpow_block_hash:
                raise HarnessError(f"Electrum {method} header hash mismatch")
            return header_hex

        def require_header_proof(method: str, value: object) -> tuple[dict, str, int]:
            if not isinstance(value, dict):
                raise HarnessError(f"Electrum {method} proof returned non-object: {value}")
            header = require_pure_header(method, value.get("header"))
            header_hash = block_hash_from_header_hex(header)
            branch = value.get("branch")
            root = value.get("root")
            if not isinstance(branch, list) or not branch:
                raise HarnessError(f"Electrum {method} proof missing branch: {value}")
            if not isinstance(root, str) or not re.fullmatch(r"[0-9a-f]{64}", root):
                raise HarnessError(f"Electrum {method} proof missing root: {value}")
            if header.lower() != self.auxpow_pure_header_hex.lower():
                raise HarnessError(f"Electrum {method} proof header mismatch")
            computed_root = electrum_merkle_root_from_branch(header_hash, height, branch)
            if computed_root != root:
                raise HarnessError(
                    f"Electrum {method} proof root mismatch: {computed_root} != {root}"
                )
            return value, root, len(branch)

        def require_headers_page(method: str, value: object, expected_hex: str) -> dict:
            if not isinstance(value, dict):
                raise HarnessError(f"Electrum {method} returned non-object: {value}")
            if int(value.get("count", -1)) != 1:
                raise HarnessError(f"Electrum {method} returned wrong count: {value}")
            header_hex = value.get("hex")
            require_pure_header(method, header_hex)
            if str(header_hex).lower() != expected_hex.lower():
                raise HarnessError(f"Electrum {method} page header mismatch")
            return value

        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=5) as sock:
            file = sock.makefile("rwb")
            subscription_header = self.electrum_request(
                file,
                79,
                "blockchain.headers.subscribe",
                [],
            )
            if not isinstance(subscription_header, dict):
                raise HarnessError(
                    f"Electrum blockchain.headers.subscribe returned non-object: "
                    f"{subscription_header}"
                )
            if int(subscription_header.get("height", -1)) != height:
                raise HarnessError(
                    f"Electrum blockchain.headers.subscribe height mismatch: "
                    f"{subscription_header}"
                )
            require_pure_header(
                "blockchain.headers.subscribe",
                subscription_header.get("hex"),
            )

            header = self.electrum_request(
                file,
                80,
                "blockchain.block.header",
                [height],
            )
            require_pure_header("blockchain.block.header", header)

            header_proof = self.electrum_request(
                file,
                81,
                "blockchain.block.header",
                [height, height],
            )
            _, header_proof_root, header_proof_branch_len = require_header_proof(
                "blockchain.block.header",
                header_proof,
            )

            headers = self.electrum_request(
                file,
                82,
                "blockchain.block.headers",
                [height, 1],
            )
            require_headers_page("blockchain.block.headers", headers, self.auxpow_pure_header_hex)
            if "branch" in headers or "root" in headers:
                raise HarnessError(
                    f"Electrum blockchain.block.headers unexpectedly returned proof: {headers}"
                )

            headers_proof = self.electrum_request(
                file,
                83,
                "blockchain.block.headers",
                [height, 1, height],
            )
            require_headers_page(
                "blockchain.block.headers",
                headers_proof,
                self.auxpow_pure_header_hex,
            )
            branch = headers_proof.get("branch")
            root = headers_proof.get("root")
            if not isinstance(branch, list) or not branch:
                raise HarnessError(
                    f"Electrum blockchain.block.headers proof missing branch: {headers_proof}"
                )
            if not isinstance(root, str) or not re.fullmatch(r"[0-9a-f]{64}", root):
                raise HarnessError(
                    f"Electrum blockchain.block.headers proof missing root: {headers_proof}"
                )
            computed_root = electrum_merkle_root_from_branch(
                self.auxpow_block_hash,
                height,
                branch,
            )
            if computed_root != root:
                raise HarnessError(
                    "Electrum blockchain.block.headers proof root mismatch: "
                    f"{computed_root} != {root}"
                )

        self.electrum_auxpow_header_methods = [
            "blockchain.headers.subscribe",
            "blockchain.block.header",
            "blockchain.block.header(cp_height)",
            "blockchain.block.headers",
            "blockchain.block.headers(cp_height)",
        ]
        self.electrum_auxpow_header_facts = {
            "pure_header_bytes": len(bytes.fromhex(self.auxpow_pure_header_hex)),
            "subscribe_height": height,
            "header_hash": self.auxpow_block_hash,
            "block_header_proof_root": header_proof_root,
            "block_header_proof_branch_len": header_proof_branch_len,
            "block_headers_proof_root": root,
            "block_headers_proof_branch_len": len(branch),
        }

    @staticmethod
    def electrum_request(file, request_id: int, method: str, params: list[object]):
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        file.write(json.dumps(request).encode() + b"\n")
        file.flush()
        line = file.readline()
        if not line:
            raise HarnessError(f"Electrum connection closed waiting for {method}")
        response = json.loads(line.decode())
        if response.get("id") != request_id:
            raise HarnessError(f"unexpected Electrum response id: {response}")
        if response.get("error"):
            raise HarnessError(f"Electrum {method} failed: {response['error']}")
        return response.get("result")

    def write_manifest(self) -> None:
        manifest = {
            "contract": {
                "document": "doc/qbit-contract.md",
                "qbit_repository": "qbit-reference",
                "qbit_commit": self.contract_commit,
                "issues": ["#2", "#4", "#6", "#12", "#14", "#20"],
            },
            "qbit_regtest": {
                "genesis": REGTEST_GENESIS,
                "height": self.best_height,
                "tip": self.best_hash,
                "p2mr_address": self.p2mr_address,
                "blocks_mined": self.args.mine_blocks,
                "fee_methods": {
                    "regtest_estimatefee_source": "relayfee",
                    "relayfee_btc_per_kb": self.electrum_relayfee_btc_kb,
                    "estimatefee_targets": self.electrum_estimatefee_targets,
                    "rest_fee_estimate_targets_sat_per_vb": self.rest_fee_estimate_targets,
                },
                "p2mr_spend": {
                    "address": self.spend_address,
                    "broadcast_method": "blockchain.transaction.broadcast",
                    "script_hash": self.spend_script_hash,
                    "electrum_scripthash": self.spend_electrum_scripthash,
                    "txid": self.spend_txid,
                    "prevout": {
                        "txid": self.spend_prevout_txid,
                        "vout": self.spend_prevout_vout,
                    },
                    "recipient_vout": self.spend_recipient_vout,
                    "confirmed_height": self.spend_confirmed_height,
                    "confirmed_block_tx_pos": self.spend_tx_block_pos,
                    "amount_sats": SPEND_AMOUNT_SATS,
                    "mempool_aggregate_surfaces": {
                        "vsize_denominator": "qbit serialized bytes under WSF=1",
                        "phases": self.mempool_aggregate_surfaces,
                    },
                    "reorg": {
                        "invalidated_block": self.reorged_block_hash,
                        "reorged_to_height": self.reorged_to_height,
                        "reorged_to_hash": self.reorged_to_hash,
                        "restored_height": self.best_height,
                    },
                },
                "auxpow_block": {
                    "hash": self.auxpow_block_hash,
                    "height": self.auxpow_block_height,
                    "raw_block_bytes": len(bytes.fromhex(self.auxpow_block_hex))
                    if self.auxpow_block_hex
                    else 0,
                    "pure_header_bytes": len(bytes.fromhex(self.auxpow_pure_header_hex))
                    if self.auxpow_pure_header_hex
                    else 0,
                    "extended_header_bytes": len(bytes.fromhex(self.auxpow_extended_header_hex))
                    if self.auxpow_extended_header_hex
                    else 0,
                    "tx_count": len(self.auxpow_txids),
                    "electrum_header_methods": self.electrum_auxpow_header_methods,
                    "electrum_header_facts": self.electrum_auxpow_header_facts,
                },
            },
            "electrs": {
                "network": "qbitregtest",
                "rest": f"127.0.0.1:{self.http_port}",
                "electrum": f"127.0.0.1:{self.electrum_port}",
                "db_dir": str(self.electrs_db),
            },
        }
        if self.fixture_export_manifest:
            manifest["fixture_exports"] = {
                "enabled": True,
                "root": str(self.fixture_export_root),
                "manifest": str(self.fixture_export_manifest),
                "issue": "#14",
                "promotion_policy": (
                    "Review qbitd-generated bytes and copy selected files into "
                    "tests/fixtures/qbit/ with manifest provenance before committing."
                ),
            }
        (self.artifacts / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def write_qbit_fixture_exports(self) -> None:
        if not self.spend_tx_hex or not self.spend_block_hex:
            raise HarnessError("fixture export requested before P2MR spend artifacts were captured")
        if (
            self.spend_tx_confirmed_verbose is None
            or self.spend_block_json is None
            or self.spend_block_header_json is None
            or self.spend_mempool_entry is None
            or self.spend_mempool_txids is None
        ):
            raise HarnessError("fixture export requested with incomplete qbit RPC samples")

        export_root = self.artifacts / "qbitd-fixtures"
        for child in ["transactions", "blocks", "rpc"]:
            (export_root / child).mkdir(parents=True, exist_ok=True)

        def write_text(relpath: str, text: str) -> None:
            (export_root / relpath).write_text(text.rstrip() + "\n")

        def write_json(relpath: str, value: object) -> None:
            (export_root / relpath).write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n"
            )

        write_text("transactions/regtest-qbitd-p2mr-spend.hex", self.spend_tx_hex)
        write_text("blocks/regtest-qbitd-p2mr-spend-block.hex", self.spend_block_hex)
        write_json(
            "rpc/regtest-qbitd-getrawtransaction-p2mr-spend.json",
            self.spend_tx_confirmed_verbose,
        )
        write_json("rpc/regtest-qbitd-getblock-p2mr-spend.json", self.spend_block_json)
        write_json(
            "rpc/regtest-qbitd-getblockheader-p2mr-spend.json",
            self.spend_block_header_json,
        )
        write_json(
            "rpc/regtest-qbitd-getmempoolentry-p2mr-spend.json",
            self.spend_mempool_entry,
        )
        write_json(
            "rpc/regtest-qbitd-getrawmempool-with-p2mr-spend.json",
            self.spend_mempool_txids,
        )

        block_version = int.from_bytes(bytes.fromhex(self.spend_block_hex[:8]), "little")
        block_txids = self.spend_block_json.get("tx", [])
        if not isinstance(block_txids, list):
            raise HarnessError(f"qbitd getblock tx field is not a list: {self.spend_block_json}")
        tx_size = len(bytes.fromhex(self.spend_tx_hex))
        block_size = len(bytes.fromhex(self.spend_block_hex))
        generation_command = (
            "./scripts/qbit-regtest-harness.py "
            "--qbit-source auto --build-qbit --build-electrs --export-qbit-fixtures"
        )
        source = {
            "kind": "qbitd-generated",
            "qbit_commit": self.contract_commit,
            "source_ref": (
                "local qbit regtest wallet and RPC samples generated by "
                "scripts/qbit-regtest-harness.py"
            ),
            "deterministic_key_material": (
                "qbitd wallet-generated P2MR keys; txids and block hashes may vary "
                "between runs, but each bundle records exact generated values"
            ),
        }
        manifest = {
            "schema_version": 1,
            "contract": {
                "document": "doc/qbit-contract.md",
                "qbit_repository": "qbit-reference",
                "qbit_commit": self.contract_commit,
                "issues": [4, 12, 14, 20, 22],
            },
            "fixtures": [
                {
                    "id": "regtest-qbitd-p2mr-spend-tx",
                    "network": "regtest",
                    "fixture_type": "transaction",
                    "path": "transactions/regtest-qbitd-p2mr-spend.hex",
                    "source": source,
                    "generation_command": generation_command,
                    "txid": self.spend_txid,
                    "address": self.spend_address,
                    "expected_parser_facts": {
                        "serialized_size": tx_size,
                        "qbit_weight": tx_size,
                        "witness_scale_factor": 1,
                        "input_count": 1,
                        "p2mr_witness": True,
                        "spent_prevout_txid": self.spend_prevout_txid,
                        "spent_prevout_vout": self.spend_prevout_vout,
                        "recipient_vout": self.spend_recipient_vout,
                        "recipient_value_sats": SPEND_AMOUNT_SATS,
                        "confirmed_height": self.spend_confirmed_height,
                    },
                    "refresh_policy": {
                        "reproduce": (
                            "Run the qbit regtest harness with --export-qbit-fixtures; "
                            "review the generated manifest for exact qbitd values."
                        ),
                        "diff_policy": (
                            "Wallet-generated txids may change. Promote only reviewed "
                            "qbitd output and keep the qbit commit/source metadata."
                        ),
                    },
                    "notes": "qbitd-generated P2MR witness spend captured from local regtest.",
                },
                {
                    "id": "regtest-qbitd-p2mr-spend-block",
                    "network": "regtest",
                    "fixture_type": "block",
                    "path": "blocks/regtest-qbitd-p2mr-spend-block.hex",
                    "source": source,
                    "generation_command": generation_command,
                    "height": self.spend_confirmed_height,
                    "blockhash": self.reorged_block_hash,
                    "expected_parser_facts": {
                        "serialized_size": block_size,
                        "qbit_weight": block_size,
                        "witness_scale_factor": 1,
                        "pure_header_bytes": 80,
                        "auxpow": bool(block_version & 0x100),
                        "tx_count": len(block_txids),
                        "contains_txid": self.spend_txid,
                    },
                    "refresh_policy": {
                        "reproduce": (
                            "Run the qbit regtest harness with --export-qbit-fixtures; "
                            "review the generated block hash and tx list."
                        ),
                        "diff_policy": (
                            "Block hashes may change with regenerated wallet data. "
                            "Keep only reviewed qbitd output."
                        ),
                    },
                    "notes": "qbitd-generated spend-confirming block captured before reorg checks.",
                },
                {
                    "id": "regtest-qbitd-getrawtransaction-p2mr-spend",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getrawtransaction-p2mr-spend.json",
                    "source": source,
                    "generation_command": generation_command,
                    "txid": self.spend_txid,
                    "expected_parser_facts": {
                        "method": "getrawtransaction",
                        "verbosity": True,
                        "txid": self.spend_txid,
                        "blockhash": self.reorged_block_hash,
                        "confirmed_height": self.spend_confirmed_height,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with --export-qbit-fixtures.",
                        "diff_policy": "Review qbitd RPC JSON shape changes before promotion.",
                    },
                    "notes": "Verbose qbitd transaction RPC sample for the P2MR spend.",
                },
                {
                    "id": "regtest-qbitd-getblock-p2mr-spend",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getblock-p2mr-spend.json",
                    "source": source,
                    "generation_command": generation_command,
                    "height": self.spend_confirmed_height,
                    "blockhash": self.reorged_block_hash,
                    "expected_parser_facts": {
                        "method": "getblock",
                        "verbosity": 1,
                        "tx_count": len(block_txids),
                        "contains_txid": self.spend_txid,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with --export-qbit-fixtures.",
                        "diff_policy": "Review qbitd RPC JSON shape changes before promotion.",
                    },
                    "notes": "qbitd block RPC sample for the P2MR spend block.",
                },
                {
                    "id": "regtest-qbitd-getblockheader-p2mr-spend",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getblockheader-p2mr-spend.json",
                    "source": source,
                    "generation_command": generation_command,
                    "height": self.spend_confirmed_height,
                    "blockhash": self.reorged_block_hash,
                    "expected_parser_facts": {
                        "method": "getblockheader",
                        "verbosity": True,
                        "header_bytes": 80,
                        "hash": self.reorged_block_hash,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with --export-qbit-fixtures.",
                        "diff_policy": "Review qbitd RPC JSON shape changes before promotion.",
                    },
                    "notes": "qbitd header RPC sample; REST/Electrum expose the pure 80-byte header.",
                },
                {
                    "id": "regtest-qbitd-getmempoolentry-p2mr-spend",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getmempoolentry-p2mr-spend.json",
                    "source": source,
                    "generation_command": generation_command,
                    "txid": self.spend_txid,
                    "expected_parser_facts": {
                        "method": "getmempoolentry",
                        "txid": self.spend_txid,
                        "vsize_policy": "qbit WSF=1 witness-inclusive serialized size",
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with --export-qbit-fixtures.",
                        "diff_policy": "Review qbitd mempool JSON shape changes before promotion.",
                    },
                    "notes": "qbitd mempool entry captured before the spend is mined.",
                },
                {
                    "id": "regtest-qbitd-getrawmempool-with-p2mr-spend",
                    "network": "regtest",
                    "fixture_type": "rpc",
                    "path": "rpc/regtest-qbitd-getrawmempool-with-p2mr-spend.json",
                    "source": source,
                    "generation_command": generation_command,
                    "txid": self.spend_txid,
                    "expected_parser_facts": {
                        "method": "getrawmempool",
                        "contains_txid": self.spend_txid,
                    },
                    "refresh_policy": {
                        "reproduce": "Regenerate with --export-qbit-fixtures.",
                        "diff_policy": "Review qbitd mempool JSON shape changes before promotion.",
                    },
                    "notes": "qbitd mempool txid snapshot captured before the spend is mined.",
                },
            ],
        }
        manifest_path = export_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        self.fixture_export_root = export_root
        self.fixture_export_manifest = manifest_path
        info(f"qbit fixture export manifest: {manifest_path}")

    def cleanup(self, success: bool) -> None:
        if self.electrs_proc and self.electrs_proc.poll() is None:
            self.stop_process(self.electrs_proc, "electrs")
        if self.qbit_proc and self.qbit_proc.poll() is None:
            try:
                self.qbit_cli_call("stop")
                self.qbit_proc.wait(timeout=15)
            except Exception:
                self.stop_process(self.qbit_proc, "qbitd")

        if success and not self.args.keep_artifacts:
            shutil.rmtree(self.artifacts, ignore_errors=True)
        elif not success:
            info(f"failure artifacts kept at {self.artifacts}")

    @staticmethod
    def stop_process(proc: subprocess.Popen, name: str) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=15)
        except Exception:
            info(f"force killing {name}")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)

    def report_failure(self, exc: BaseException) -> None:
        print(f"\n[qbit-harness] FAILED: {exc}", file=sys.stderr)
        print(f"\n[qbit-harness] qbitd log tail ({self.qbit_log}):", file=sys.stderr)
        print(tail_file(self.qbit_log), file=sys.stderr)
        print(f"\n[qbit-harness] electrs log tail ({self.electrs_log}):", file=sys.stderr)
        print(tail_file(self.electrs_log), file=sys.stderr)
        if self.build_log.exists():
            print(f"\n[qbit-harness] build log tail ({self.build_log}):", file=sys.stderr)
            print(tail_file(self.build_log), file=sys.stderr)


def main() -> int:
    args = parse_args()
    if args.export_qbit_fixtures:
        args.keep_artifacts = True
    harness = Harness(args)
    success = False
    try:
        harness.run()
        success = True
        return 0
    except BaseException as exc:
        harness.report_failure(exc)
        return 1
    finally:
        harness.cleanup(success)


if __name__ == "__main__":
    raise SystemExit(main())
