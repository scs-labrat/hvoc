"""Auto-bootstrap veilid-server on startup.

Detects, builds (if necessary), and starts veilid-server before the app
enters curses mode so all build output is visible in the terminal.
"""

import os
import platform
import shutil
import socket
import subprocess
import sys
import time

_VEILID_PORT = 5959
_STARTUP_TIMEOUT = 15  # seconds to wait for daemon to come up

# Relative to project root
_LOCAL_BUILD_DIR = os.path.join(os.path.dirname(__file__), "veilid")
_EXE = ".exe" if platform.system() == "Windows" else ""
_LOCAL_BINARY = os.path.join(
    _LOCAL_BUILD_DIR, "target", "release", f"veilid-server{_EXE}"
)
_LOG_FILE = os.path.join(os.path.dirname(__file__), "veilid-server.log")
_CLONE_URL = "https://gitlab.com/scs-labrat/veilid.git"


def _port_open(host: str = "localhost", port: int = _VEILID_PORT) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _find_binary() -> str | None:
    """Return path to veilid-server binary, or None if not found."""
    # 1. Check PATH
    found = shutil.which("veilid-server")
    if found:
        return found
    # 2. Check local build
    if os.path.isfile(_LOCAL_BINARY):
        return _LOCAL_BINARY
    return None


def _build_binary() -> str:
    """Clone (if needed) and build veilid-server. Returns binary path."""
    # Verify cargo is available
    if shutil.which("cargo") is None:
        print(
            "ERROR: 'cargo' not found. Install Rust from https://rustup.rs/ "
            "to build veilid-server.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Clone if needed (check for Cargo.toml to detect empty/incomplete dir)
    cargo_toml = os.path.join(_LOCAL_BUILD_DIR, "Cargo.toml")
    if not os.path.isfile(cargo_toml):
        if os.path.isdir(_LOCAL_BUILD_DIR):
            shutil.rmtree(_LOCAL_BUILD_DIR)
        print(f"Cloning veilid into {_LOCAL_BUILD_DIR} ...")
        subprocess.check_call(
            ["git", "clone", _CLONE_URL, _LOCAL_BUILD_DIR],
        )

    # Build
    print("Building veilid-server (this may take a while) ...")
    subprocess.check_call(
        ["cargo", "build", "--release", "-p", "veilid-server"],
        cwd=_LOCAL_BUILD_DIR,
    )

    if not os.path.isfile(_LOCAL_BINARY):
        print(
            f"ERROR: Build succeeded but binary not found at {_LOCAL_BINARY}",
            file=sys.stderr,
        )
        sys.exit(1)

    return _LOCAL_BINARY


def _start_daemon(binary: str) -> subprocess.Popen:
    """Start veilid-server as a background process."""
    print(f"Starting veilid-server from {binary} ...")
    log = open(_LOG_FILE, "a")

    # Platform-specific detach: Unix uses setsid, Windows uses CREATE_NEW_PROCESS_GROUP
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [binary, "-s", "client_api.network_enabled=true"],
        stdout=log,
        stderr=log,
        **kwargs,
    )

    # Wait for the port to come up
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if _port_open():
            print("veilid-server is ready.")
            return proc
        time.sleep(0.5)

    print(
        f"ERROR: veilid-server did not start within {_STARTUP_TIMEOUT}s. "
        f"Check {_LOG_FILE} for details.",
        file=sys.stderr,
    )
    proc.kill()
    sys.exit(1)


def ensure_veilid_server() -> tuple[subprocess.Popen | None, bool]:
    """Ensure veilid-server is running.

    Returns (proc, we_started) where *proc* is the Popen handle if we
    launched the daemon ourselves, and *we_started* is True in that case.
    """
    # Already running?
    if _port_open():
        print("veilid-server already running on port 5959.")
        return None, False

    # Find or build the binary
    binary = _find_binary()
    if binary is None:
        binary = _build_binary()

    proc = _start_daemon(binary)
    return proc, True


def stop_veilid_server(proc: subprocess.Popen | None, we_started: bool):
    """Stop veilid-server if we started it."""
    if not we_started or proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
