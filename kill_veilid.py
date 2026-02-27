#!/usr/bin/env python3
"""Kill all processes associated with veilid-irc for a clean restart.

Finds and terminates:
  - python irc_main.py (the IRC app itself)
  - veilid-server (the Veilid daemon)

Usage:
  python kill_veilid.py          Show what would be killed
  python kill_veilid.py --kill   Actually kill them
  python kill_veilid.py --force  Force kill (SIGKILL / taskkill /F)
"""

import argparse
import os
import platform
import signal
import subprocess
import sys


IS_WINDOWS = platform.system() == "Windows"

# Process name patterns to match
PATTERNS = [
    "veilid-server",
    "veilid_server",
    "irc_main.py",
    "irc_main",
]


def find_processes():
    """Return list of (pid, cmdline) for matching processes."""
    my_pid = os.getpid()
    found = []

    if IS_WINDOWS:
        # Use WMIC for full command lines
        try:
            out = subprocess.check_output(
                ["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
                text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.strip().splitlines():
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                cmdline = ",".join(parts[1:-1]).strip()
                try:
                    pid = int(parts[-1].strip())
                except ValueError:
                    continue
                if pid == my_pid:
                    continue
                cmd_lower = cmdline.lower()
                for pat in PATTERNS:
                    if pat.lower() in cmd_lower:
                        found.append((pid, cmdline))
                        break
        except FileNotFoundError:
            # WMIC not available, fall back to tasklist
            try:
                out = subprocess.check_output(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    text=True, stderr=subprocess.DEVNULL,
                )
                for line in out.strip().splitlines():
                    parts = line.strip('"').split('","')
                    if len(parts) < 2:
                        continue
                    name = parts[0]
                    try:
                        pid = int(parts[1])
                    except ValueError:
                        continue
                    if pid == my_pid:
                        continue
                    for pat in PATTERNS:
                        if pat.lower() in name.lower():
                            found.append((pid, name))
                            break
            except Exception:
                pass
    else:
        # Unix: use ps
        try:
            out = subprocess.check_output(
                ["ps", "aux"], text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.strip().splitlines()[1:]:  # skip header
                parts = line.split(None, 10)
                if len(parts) < 11:
                    continue
                try:
                    pid = int(parts[1])
                except ValueError:
                    continue
                if pid == my_pid:
                    continue
                cmdline = parts[10]
                cmd_lower = cmdline.lower()
                for pat in PATTERNS:
                    if pat.lower() in cmd_lower:
                        found.append((pid, cmdline))
                        break
        except Exception:
            pass

    return found


def kill_process(pid, force=False):
    """Kill a process by PID. Returns True on success."""
    try:
        if IS_WINDOWS:
            args = ["taskkill"]
            if force:
                args.append("/F")
            args.extend(["/PID", str(pid)])
            subprocess.check_call(args, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        else:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.kill(pid, sig)
        return True
    except Exception as e:
        print(f"  Failed to kill PID {pid}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Find and kill veilid-irc related processes",
    )
    parser.add_argument("--kill", action="store_true",
                        help="Actually terminate the processes")
    parser.add_argument("--force", action="store_true",
                        help="Force kill (SIGKILL / taskkill /F)")
    args = parser.parse_args()

    procs = find_processes()

    if not procs:
        print("No veilid-irc processes found. Clean state.")
        return

    print(f"Found {len(procs)} process(es):\n")
    for pid, cmdline in procs:
        # Truncate long command lines
        display = cmdline if len(cmdline) < 100 else cmdline[:97] + "..."
        print(f"  PID {pid:>6}  {display}")

    if not args.kill and not args.force:
        print(f"\nRun with --kill to terminate, or --force to force kill.")
        return

    print()
    killed = 0
    for pid, cmdline in procs:
        name = cmdline.split()[0] if cmdline else str(pid)
        if len(name) > 40:
            name = f"...{name[-37:]}"
        ok = kill_process(pid, force=args.force)
        if ok:
            killed += 1
            print(f"  Killed PID {pid} ({name})")
        else:
            print(f"  FAILED PID {pid} ({name})")

    print(f"\nDone. Killed {killed}/{len(procs)} processes.")

    if killed > 0:
        print("You can now restart with: python irc_main.py --nick <your_nick>")


if __name__ == "__main__":
    main()
