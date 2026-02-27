# Veilid Overlay Chat (VOC)

**Decentralised, encrypted, serverless group chat over the Veilid P2P network.**

```
  ╦  ╦┌─┐┬┬  ┬┌┬┐   ╔═╗╦  ╦╔═╗╦═╗╦  ╔═╗╦ ╦  ╔═╗╦ ╦╔═╗╔╦╗
  ╚╗╔╝├┤ ││  │ ││   ║ ║╚╗╔╝║╣ ╠╦╝║  ╠═╣╚╦╝  ║  ╠═╣╠═╣ ║
   ╚╝ └─┘┴┴─┘┴─┴┘   ╚═╝ ╚╝ ╚═╝╩╚═╩═╝╩ ╩ ╩   ╚═╝╩ ╩╩ ╩ ╩
```

VOC brings the familiar IRC command set to a fully peer-to-peer architecture. There are no servers to compromise, no accounts to breach, no metadata to harvest, and no message logs to subpoena. Every byte travels through Veilid's onion-routed private route system — the network itself cannot tell who is talking to whom.

If you know IRC, you already know VOC. If you don't, think of it as Slack or Discord, except nobody owns the server because there isn't one.

---

## Table of Contents

- [Privacy & Security Model](#privacy--security-model)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [The Directory System & Short Join Codes](#the-directory-system--short-join-codes)
- [User Interface](#user-interface)
- [Command Reference](#command-reference)
- [Architecture Overview](#architecture-overview)
- [Logging & Troubleshooting](#logging--troubleshooting)
- [Utilities](#utilities)
- [Project Structure](#project-structure)
- [Known Limitations](#known-limitations)

---

## Privacy & Security Model

### What Veilid Gives You

[Veilid](https://veilid.com) is a peer-to-peer application framework built for privacy from the ground up. It provides the transport layer that makes VOC possible.

**Private Routes** — Every user creates an ephemeral private route, analogous to a Tor hidden service. Other users send messages *to* your route without ever learning your IP address. Routes are rotated and are not linkable across sessions.

**Onion-Style Routing** — Messages are relayed through multiple intermediate nodes. Each hop can only see the previous hop and the next hop. No single node ever sees both the sender and the recipient.

**Encrypted DHT** — Channel state lives on Veilid's distributed hash table. Records are signed with a shared-owner keypair so only participants who hold the channel key can read or write entries. The DHT stores *presence data* (who is online right now), not message content.

**Zero Global Identity** — There is no user database anywhere. Your identity is a locally generated cryptographic keypair that never leaves your machine. There is nothing to hack, nothing to leak, nothing to subpoena.

### What VOC Adds

**No Message Persistence** — Chat messages travel directly between online clients via Veilid `app_message` calls. They are compressed with zlib, routed through encrypted private routes, and exist only in each client's local memory. There is no DHT message log, no server archive, and no way to retrieve a message you were not online to receive. When you close the app, the conversation is gone.

**Channel Isolation** — Each channel is an independent DHT record with its own keypair. Joining one channel reveals nothing about your membership in others. There is no global channel list or user roster.

**Shared-Owner Access Control** — When a channel is created, VOC generates a keypair and encodes it into a `CHAN:` share string. That string *is* the channel password. Anyone who possesses it can join and participate. Anyone who doesn't cannot discover or access the channel. Distribute the share string through whatever secure channel you trust (Signal, encrypted email, in person, etc).

**Heartbeat Presence** — Each member writes a timestamped entry to their DHT slot every 30 seconds containing only their nickname and an opaque route blob. Members who go silent for 90 seconds are pruned. This is the only data that persists on the network, and it reveals nothing about your real identity or location.

### Threat Model — What VOC Does Not Protect Against

**Endpoint Compromise** — If an attacker has access to your device, they can read messages from memory or capture keystrokes. VOC does not protect against local compromise.

**Global Traffic Analysis** — A state-level adversary monitoring a substantial fraction of all internet traffic could theoretically perform timing correlation on Veilid's relay network. This is a fundamental limitation shared by all overlay networks including Tor.

**Nickname Impersonation** — VOC has no account system. Anyone can claim any nickname. If verifiable identity matters, exchange cryptographic keys out of band.

**Share String Leakage** — The `CHAN:` and `DIR:` share strings contain write keypairs. If they leak to someone you do not trust, that person gains full channel access. Treat them as passwords.

**Directory Visibility** — Publishing a channel to a directory makes its name, topic, and full share string visible to everyone who has the `DIR:` string.

---

## Requirements

- **Python 3.12+** (3.13 or 3.14 recommended)
- **veilid-server** — the Veilid network daemon, running locally on port 5959
- **Operating System** — Windows 10/11, Linux, or macOS

Python dependencies (installed via pip):

```
veilid>=0.4.0
textual>=1.0.0
qrcode[pil]>=7.0
pyzbar>=0.1.9
```

The terminal UI is built on [Textual](https://textual.textualize.io/), which works natively on all three platforms without curses. QR code support uses [qrcode](https://pypi.org/project/qrcode/) for generation and [pyzbar](https://pypi.org/project/pyzbar/) for decoding.

---

## Installation

### Step 1 — Install veilid-server

veilid-server is the local daemon that connects your machine to the Veilid P2P network. VOC communicates with it over a local websocket on port 5959.

**Windows** (build from source — requires the [Rust toolchain](https://rustup.rs/)):

```powershell
git clone https://gitlab.com/scs-labrat/veilid.git
cd veilid
cargo build --release -p veilid-server
# The binary is now at target\release\veilid-server.exe
# Either add that directory to your PATH or copy the binary somewhere convenient.
```

> The final linking step (522/523) can take 5–15 minutes on Windows. This is normal — the MSVC linker is single-threaded and Veilid is a large project.

**Linux (Debian / Ubuntu)**:

```bash
# Build from source
git clone https://gitlab.com/scs-labrat/veilid.git
cd veilid
cargo build --release -p veilid-server
sudo cp target/release/veilid-server /usr/local/bin/
```

**macOS**:

```bash
git clone https://gitlab.com/scs-labrat/veilid.git
cd veilid
cargo build --release -p veilid-server
sudo cp target/release/veilid-server /usr/local/bin/
```

### Step 2 — Install VOC

```bash
git clone <this-repo>
cd voc

# Create a virtual environment (recommended)
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
```

### Step 3 — Start veilid-server

```bash
veilid-server &
```

On its first run, veilid-server needs 30–90 seconds to bootstrap onto the P2P network. VOC waits automatically — you will see attachment state updates in the status bar (`Attaching → AttachedGood → OverAttached`).

VOC also tries to auto-detect and launch veilid-server if it finds the binary on your PATH or in a `veilid/` subdirectory. If it starts veilid-server for you, it will also shut it down on exit.

---

## Quick Start

### Create a Channel and Invite Someone

```bash
# Alice starts VOC and creates a channel called "general"
python irc_main.py --nick alice --create general --topic "Welcome to VOC"
```

VOC prints a `CHAN:` share string. Alice copies it and sends it to Bob through any trusted side channel (Signal, email, face to face, QR code, etc).

```bash
# Bob joins using the share string
python irc_main.py --nick bob --join "CHAN:eyJrIjoi..."
```

Both users are now chatting. Messages flow directly between them through Veilid's encrypted relay network.

### Join with a Short Code (Directory)

If your community has set up a directory, you can skip the long share strings entirely:

```bash
# Bob was given a DIR: string once for the community directory
python irc_main.py --nick bob --dir "DIR:eyJrIjoi..."
```

Then inside the app:

```
/rooms              → lists all published channels with 4-char codes
/join A7K2          → joins by short code
/join #general      → joins by channel name
```

### Command-Line Options

| Flag | Description |
|------|-------------|
| `--nick NAME` | Your nickname (prompted interactively if not given) |
| `--create NAME` | Create a channel on startup |
| `--topic TEXT` | Set the topic for `--create` |
| `--join SHARE` | Join a channel by `CHAN:` share string |
| `--dir SHARE` | Join a directory by `DIR:` share string on startup |

---

## The Directory System & Short Join Codes

Long `CHAN:` share strings are cumbersome to pass around. The directory solves this with memorable 4-character join codes.

**How it works:**

1. Someone creates a directory with `/dir create`. This produces a single `DIR:` share string that is distributed to the community once.
2. Anyone with the `DIR:` string can publish channels (`/publish`) and browse them (`/rooms`).
3. Each published channel gets a deterministic 4-character code (e.g. `A7K2`) derived from the SHA-256 hash of its share string, encoded in a base-32 character set (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789` — no ambiguous characters like `0/O`, `1/I/L`).
4. Users join by typing `/join A7K2` instead of pasting a 200-character share string.

**Directory entries contain:** publisher nickname, channel name, topic, full `CHAN:` share string, approximate member count, and a timestamp. Entries older than 24 hours are automatically reclaimed so the directory stays fresh. A single directory supports up to 63 channels.

> **Privacy note:** Publishing to a directory exposes your channel's share string to everyone with the `DIR:` string. Only publish to directories whose membership you trust.

---

## User Interface

VOC uses a three-panel terminal interface:

```
┌──────────┬──────────────────────────────┬──────────┐
│ CHANNELS │  #general — Welcome to VOC   │  USERS   │
│          ├──────────────────────────────┤          │
│ #general │ [14:32] <alice> hey everyone │ @alice   │
│ #random  │ [14:32] <bob> yo!            │  bob     │
│          │ [14:33] * alice waves        │  charlie │
│          │ [14:33] --- charlie has quit  │          │
│          ├──────────────────────────────┤          │
│          │ > type here_                 │          │
├──────────┴──────────────────────────────┴──────────┤
│ Connected to Veilid network ─ nick: alice ─ 1 ch  │
└───────────────────────────────────────────────────┘
```

**Left panel** — Joined channels. Click to switch, or use `Ctrl+N` / `Ctrl+P`.

**Centre panel** — Message history (top) and input field (bottom). The topic bar shows the active channel's name and topic.

**Right panel** — Users in the active channel. Operators are prefixed with `@`.

**Status bar** — Connection state, nickname, channel count.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+N` | Next channel |
| `Ctrl+P` | Previous channel |
| `Ctrl+Q` | Quit (graceful shutdown with quit notice) |
| `Enter` | Send message or execute `/command` |

### Message Prefixes

| Prefix | Meaning |
|--------|---------|
| `<nick>` | Normal message |
| `* nick` | Action (`/me`) |
| `-nick-` | Notice |
| `>>>` | Join |
| `<<<` | Part or quit |
| `<<!` | Kick |
| `>>>` (magenta) | Invite received |
| `---` (dim) | Away status change |
| `---` (cyan) | Mode change |
| `[sys]` (yellow) | System / info |

---

## Command Reference

Commands are case-insensitive. Arguments in `<angle brackets>` are required; `[square brackets]` are optional. Where a command takes an optional `#channel`, it defaults to the active channel.

---

### I. Connection & Setup

**`/server`** — Display veilid-server connection info (address, port, protocol). VOC connects to `localhost:5959` via JSON-RPC over websocket. If an alternative address is given as an argument, VOC reconnects to that server.

**`/quit [message]`** — Broadcast a quit notice to all channels, clean up Veilid resources, and exit. If a message is provided it is shown to other users (e.g. `/quit goodnight all`). Also triggered by `Ctrl+Q`.

**`/nick <newnick>`** — Change your nickname. The change is broadcast to every channel you are in. Other users see `"oldnick is now known as newnick"`. Nicknames are freeform strings with no registration — pick whatever you like.

**`/user`** — Display your current identity: nickname, away status, channels joined, and profile key hash (if the identity module is active). Purely informational.

**`/ping <nick>`** — Send a timestamped ping to another user through Veilid's private route system and measure the round-trip time. Displays latency in milliseconds. Useful for verifying connectivity.

---

### II. Channel Operations

**`/create <name> [topic]`** — Create a new channel. You automatically become the channel operator (`@`). VOC prints the `CHAN:` share string which you distribute to invite others. The optional topic is set on the channel metadata. Example: `/create general Welcome to our community!`

**`/join <target>`** — Join a channel. The target can be any of four formats:
- A full share string: `/join CHAN:eyJrIjoi...`
- A 4-character directory code: `/join A7K2`
- A channel name (requires directory): `/join #general`
- A QR code image: `/join C:\Users\you\Downloads\invite.png`

When joining by code or name, VOC looks up the full share string from the directory automatically. When given an image path, VOC decodes the QR code and extracts the `CHAN:` string.

**`/part [#channel] [message]`** — Leave a channel. Your departure message (if any) is broadcast to remaining members. If no channel is specified, you leave the active channel. Your DHT slot is cleared on exit. Alias: `/leave`.

**`/topic [#channel] [new topic]`** — View or change the channel topic. If mode `+t` is set (the default), only operators can change the topic. Without arguments, displays the current topic.

**`/invite <nick> [#channel]`** — Send an invitation to another user. The recipient receives the channel name and the full `CHAN:` share string, so they can join directly from the invite notification. You must share at least one channel with the target user for the message to be delivered.

**`/kick [#channel] <nick> [reason]`** — Remove a user from the channel (requires operator `@` status). The kicked user's DHT slot is cleared and they receive a kick notification with the optional reason. Example: `/kick troll Please read the rules`.

**`/ban [#channel] <pattern>`** — Ban a nickname pattern (requires operator). Supports shell-style wildcards via `fnmatch`: `/ban troll*` bans all nicks starting with "troll". `/ban *` bans everyone except operators. Banned users are rejected on join and kicked if already present.

**`/unban [#channel] <pattern>`** — Remove a ban pattern (requires operator). The pattern must match exactly what was set with `/ban`.

**`/kickban [#channel] <nick> [reason]`** — Kick and ban in a single command (requires operator). Alias: `/kb`.

**`/mode [#channel] [+/-modes] [nick]`** — View or modify channel modes and per-user flags. Without arguments, shows the current mode string and operator list. See the [Channel Modes](#channel-modes) section below for the full mode reference. Examples:
```
/mode                          → show modes for active channel
/mode #general +m              → enable moderated mode
/mode #general +o bob          → make bob an operator
/mode #general -t+s            → remove topic-lock, add secret
```

---

### III. Private Communication

**`/msg <nick> <text>`** — Send a private message to a user. The message is routed through any channel you share — you must be in at least one common channel. The recipient sees the message prefixed with your nick. Alias: `/privmsg`.

**`/query <nick> [text]`** — Identical to `/msg`. Opens or continues a private conversation.

**`/notice <target> <text>`** — Send a notice to a nick or `#channel`. Notices are conventionally non-conversational (automated replies, bots, system announcements) and should not trigger auto-responses.

**`/me <action text>`** — Send an action. Displayed as `* yournick does something` instead of the usual `<yournick> text` format.

**`/ignore [nick]`** — Toggle ignore for a user. Messages from ignored nicks are silently dropped before display. Run without arguments to see the current ignore list. Useful for filtering noise without involving channel operators.

---

### IV. Information & Status

**`/who [#channel]`** — List all users in a channel with their away status and time since last heartbeat. Defaults to the active channel. Operators are marked with `@`.

**`/whois <nick>`** — Show detailed information about a user: every channel they share with you, away message, last-seen timestamp, and profile key (if set). This only reveals information from channels you have in common — you cannot query users in channels you haven't joined.

**`/whowas <nick>`** — Not available. VOC has no server-side history. Returns an explanation.

**`/list`** — List all channels you have joined with member count, mode string, and topic.

**`/names [#channel]`** — List the nicknames in a channel. Operators are prefixed with `@`.

**`/away [message]`** — Set your away status. Your away message is included in your DHT heartbeat and shown in `/who` and `/whois` output. Other users are notified of your status change. Run without arguments to mark yourself as returned.

**`/userhost <nick>`** — Not meaningful in VOC. Veilid does not expose IP addresses or hostnames. Returns an explanation of why.

**`/time`** — Display the current local time.

**`/version`** — Display the VOC version string.

**`/admin`** — Not applicable. There is no server administrator in a peer-to-peer network. Returns an explanation.

**`/info`** — Show connection details: VOC version, your nickname, session uptime, number of channels, total known peers, and the veilid-server address.

**`/motd`** — Redisplay the Message of the Day banner.

**`/stats`** — Show session statistics: uptime, channels joined, total peers across all channels, and ignored users.

---

### V. Veilid-Specific Commands

These commands have no traditional IRC equivalent. They exist because of VOC's decentralised architecture.

**`/share [#channel]`** — Display the `CHAN:` share string for a channel. This is the string you give to someone so they can `/join`. Without arguments, shows the share string for the active channel.

**`/share [#channel] qr`** — Generate a branded QR code PNG for the channel's share string. The image is saved to `./qrcodes/` and can be shared as a scannable invite. Example: `/share qr` or `/share #random qr`.

**`/switch <#channel>`** — Switch the active channel (the one displayed in the centre panel and targeted by commands that default to the active channel). Aliases: `/s`, `/w`.

**`/clear`** — Clear the message history for the active channel. This is purely local — it does not affect other users' message buffers.

**`/help`** — Display the complete command reference, organised by category, inside the chat window.

---

### VI. Directory Commands

The directory provides a community-wide channel listing with short join codes, eliminating the need to pass around long `CHAN:` share strings.

**`/dir`** — Show directory status: whether a directory is configured, its DHT key, and available subcommands.

**`/dir create`** — Create a new directory. Prints a `DIR:` share string. Distribute this string once to your community — anyone who has it can browse and publish channels.

**`/dir join <DIR:...>`** — Join an existing directory using its share string or a QR code image (e.g. `/dir join invite.png`). The directory is persisted locally so you don't need to rejoin on restart.

**`/dir share`** — Display the `DIR:` share string for the current directory (so you can share it with others).

**`/dir share qr`** — Generate a branded QR code PNG for the directory's share string. Saved to `./qrcodes/`.

**`/rooms`** — Browse all channels published to the directory. For each channel, shows: the 4-character join code, channel name, publisher nick, topic, member count, and how recently it was published. Example output:
```
Directory (3 channels):
  [A7K2] #general (4 users) by alice — Welcome to VOC [5m ago]
  [R9XT] #random (2 users) by bob — Off-topic chat [12m ago]
  [3FHN] #dev (1 users) by charlie — Development discussion [1h ago]
Join with: /join <CODE>
```

**`/publish [topic]`** — Publish the active channel to the directory. Returns the 4-character join code. If a topic argument is provided, it overrides the channel's current topic in the directory listing. The publish runs in the background so the UI stays responsive.

**`/unpublish`** — Remove the active channel from the directory. Its slot becomes available for other channels.

---

### VII. QR Codes

VOC can generate and decode QR codes for sharing channels and directories without copy-pasting long strings.

**Generating QR codes:**

**`/share [#channel] qr`** — Save a branded QR code PNG for a channel's share string.

**`/dir share qr`** — Save a branded QR code PNG for the directory's share string.

QR images are saved to `./qrcodes/` with filenames like `voc_channel_general.png` and `voc_directory_Community.png`. They use VOC branding (cyan on dark background) and include a header and channel name label.

**Scanning QR codes:**

**`/scan <path>`** — The all-in-one decode command. Reads a QR code image, detects whether it contains a `CHAN:` or `DIR:` string, and auto-joins accordingly. Example:
```
/scan C:\Users\you\Downloads\voc_channel_general.png
```

`/join` and `/dir join` also accept image paths directly — VOC detects image file extensions (`.png`, `.jpg`, `.bmp`, `.gif`, `.webp`) and decodes them automatically before joining.

**CLI support** — QR images work at startup too:
```bash
python irc_main.py --nick alice --join qrcodes/voc_channel_general.png
python irc_main.py --nick alice --dir qrcodes/voc_directory_community.png
```

**Dependencies** — QR generation uses `qrcode[pil]`. Decoding uses `pyzbar` (preferred) with OpenCV as a fallback. Install with `pip install qrcode[pil] pyzbar`.

> **Note (Windows):** `pyzbar` requires the zbar shared library. If it fails to import, either install `pip install pyzbar[scripts]` or rely on the OpenCV fallback by installing `pip install opencv-python-headless`.

---

### Channel Modes

Set modes with `/mode #channel +/-<flags> [nick]`. Changing modes requires operator (`@`) status.

#### Channel Flags

| Flag | Name | Default | Description |
|------|------|---------|-------------|
| `n` | No External Messages | **On** | Only channel members can send messages to the channel. |
| `t` | Topic Protection | **On** | Only operators can change the topic. |
| `m` | Moderated | Off | Only operators and voiced (`+v`) users can speak. Everyone else is read-only. |
| `s` | Secret | Off | Channel is hidden from `/list` output. |
| `p` | Private | Off | Channel is marked as private (informational). |
| `i` | Invite-Only | Off | Users must receive an `/invite` before they can join. |

#### Per-User Flags

| Flag | Name | Description |
|------|------|-------------|
| `o` | Operator | Full channel control: kick, ban, set modes, change topic. The channel creator is automatically `+o`. Grant with `/mode #chan +o nick`. |
| `v` | Voice | Allows a user to speak in moderated (`+m`) channels. Grant with `/mode #chan +v nick`. |

#### Examples

```
/mode #general                → view current modes and ops
/mode #general +m             → enable moderated mode
/mode #general -t             → let anyone set the topic
/mode #general +o bob         → make bob an operator
/mode #general +v charlie     → give charlie voice in moderated channel
/mode #general +ms            → enable moderated + secret
/mode #general -n+i           → remove no-external, add invite-only
```

---

### Unavailable IRC Operator Commands

The following traditional IRC commands are recognised but return an explanation that server-level operator powers do not exist in a peer-to-peer network:

`/kill`, `/kline`, `/gline`, `/shun`, `/oper`

---

### Command Aliases

| Alias | Resolves To |
|-------|-------------|
| `/leave` | `/part` |
| `/privmsg` | `/msg` |
| `/kb` | `/kickban` |
| `/s` | `/switch` |
| `/w` | `/switch` |

---

## Architecture Overview

### Channel DHT Record

Each channel is a Veilid DHT record with 32 subkeys using the shared-owner model:

```
Subkey 0:     Channel metadata
              {name, topic, modes, ops, bans, created, v}

Subkeys 1-31: Member presence slots
              {nick, route_blob, profile_key, timestamp, away}
```

The shared-owner keypair (embedded in the `CHAN:` share string) grants read and write access to the entire record. This is the channel's access control mechanism.

Maximum 31 concurrent members per channel.

### Message Flow

Messages do not touch the DHT. They travel directly between clients:

```
Alice types "hello"
  │
  ├─ compress(zlib)
  │
  ├─► app_message → Bob's private route   → [Veilid relay hops] → Bob
  ├─► app_message → Charlie's private route → [Veilid relay hops] → Charlie
  └─► app_message → Dave's private route   → [Veilid relay hops] → Dave
```

Each `app_message` is onion-routed through Veilid's relay network. No single relay sees both sender and receiver.

### Presence Discovery

1. Every 30 seconds, each member writes a heartbeat to their DHT slot (nickname + route blob + timestamp).
2. Every 5 seconds, each client polls all 31 slots to discover new members and detect departures.
3. Members silent for 90+ seconds are pruned from the local roster.
4. When a new member is discovered, their route blob is imported and they become reachable for `app_message`.

### Directory DHT Record

The directory is a separate DHT record with 64 subkeys:

```
Subkey 0:      Directory header {name, type, created, v}
Subkeys 1-63:  Channel entries
               {nick, name, topic, share, short_code, members, timestamp}
```

Entries older than 24 hours are automatically reclaimed when new channels are published.

---

## Logging & Troubleshooting

VOC writes detailed logs to `./logs/veilid-irc.log` with per-module tags:

```
2026-02-27 15:08:55 INFO  [virc.net] Connecting to veilid-server localhost:5959...
2026-02-27 15:08:55 INFO  [virc.net] Routing context created (safety is default)
2026-02-27 15:08:55 INFO  [virc.net] Current attachment state: Attaching
2026-02-27 15:09:03 INFO  [virc.net] Attachment state: Attaching → OverAttached
2026-02-27 15:09:03 INFO  [virc.net] Network ready! State: OverAttached
2026-02-27 15:09:04 INFO  [virc.net] Private route allocated on attempt 1
2026-02-27 15:09:04 INFO  [virc.channel] Creating channel #general
2026-02-27 15:09:05 INFO  [virc.directory] Publishing channel #general (code=A7K2)
```

**Log configuration:**
- File: `./logs/veilid-irc.log`
- Rotation: 5 MB per file, 3 backups
- Level: DEBUG (everything is logged)
- Stderr: set environment variable `VIRC_STDERR_LOG=1` to also print WARNING+ to stderr

### Common Issues

**"Waiting for Veilid network (this may take a minute)..."** — veilid-server is bootstrapping. This is normal on cold start and takes 30–90 seconds. VOC waits up to 120 seconds with automatic retries.

**"unable to allocate route until we have a valid PublicInternet network class"** — Same cause as above. The node is still discovering peers. VOC retries route allocation up to 10 times with 3-second intervals.

**UI unresponsive during `/publish` or `/rooms`** — These commands scan up to 63 DHT subkeys. They run as background tasks with 10-second per-call timeouts, but may take a while on slow networks. Check the log for per-subkey progress.

**Other users not appearing** — Member discovery polls every 5 seconds. Allow up to 10 seconds for a new joiner to appear. Verify both users have identical `CHAN:` share strings.

**"AssertionError: Should have released routing context"** — A resolved issue caused by `with_default_safety()` creating orphan handles. VOC now uses only `new_routing_context()` since safety routing is the default in modern Veilid. Ensure all files are updated.

---

## Utilities

### kill_veilid.py

Finds and kills all VOC-related processes for a clean restart:

```bash
python kill_veilid.py            # Dry run — show what's running
python kill_veilid.py --kill     # Graceful terminate (SIGTERM / taskkill)
python kill_veilid.py --force    # Hard kill (SIGKILL / taskkill /F)
```

Matches processes containing `veilid-server`, `veilid_server`, or `irc_main.py`. Works on Windows, Linux, and macOS.

---

## Project Structure

```
voc/
├── irc_main.py          Entry point, CLI, 48-command dispatcher     (1485 lines)
├── irc_ui.py            Textual terminal UI                         (593 lines)
├── irc_channel.py       Channel management, DHT slots, modes, bans  (901 lines)
├── irc_directory.py     Channel directory with 4-char short codes   (349 lines)
├── irc_net.py           Veilid connection, network wait, routing    (273 lines)
├── irc_log.py           Centralised rotating file logger            (79 lines)
├── irc_qr.py            QR code generation and decoding             (280 lines)
├── identity.py          Persistent cryptographic identity           (136 lines)
├── bootstrap.py         Auto-detect and start veilid-server         (148 lines)
├── kill_veilid.py       Process cleanup utility                     (178 lines)
├── requirements.txt     Python dependencies
├── .gitignore           Excludes logs/, qrcodes/, __pycache__/, .venv
├── logs/                Created at runtime
│   └── veilid-irc.log
└── qrcodes/             Created by /share qr and /dir share qr
    └── voc_channel_general.png
```

---

## Configuration Constants

Adjustable in their respective source files:

| Constant | File | Default | Purpose |
|----------|------|---------|---------|
| `MAX_MEMBERS` | `irc_channel.py` | 31 | Max concurrent members per channel |
| `HEARTBEAT_INTERVAL` | `irc_channel.py` | 30s | How often presence is refreshed on DHT |
| `STALE_TIMEOUT` | `irc_channel.py` | 90s | Silence threshold before a member is pruned |
| `NETWORK_READY_TIMEOUT` | `irc_net.py` | 120s | Max wait for veilid-server to join the network |
| `DHT_TIMEOUT` | `irc_directory.py` | 10s | Per-operation timeout for directory DHT calls |
| `STALE_AGE` | `irc_directory.py` | 86400s (24h) | Directory entries older than this are reclaimable |
| `MAX_SLOTS` | `irc_directory.py` | 63 | Max published channels per directory |

---

## Known Limitations

- **31 members per channel** — constrained by DHT subkey count. Create multiple channels for larger groups.
- **No offline messages** — messages exist only in transit and in recipient memory. If you are offline, you miss them.
- **No message history** — closing the app clears all messages. This is by design.
- **No nickname authentication** — anyone can claim any name. Verify identities out of band if it matters.
- **Single veilid-server** — VOC connects to `localhost:5959` only.
- **DHT propagation delay** — on sparse networks, DHT writes can take several seconds to become visible to other nodes.
- **Directory entries expire** — unpublished channels disappear from the directory after 24 hours. Re-publish periodically if needed.

---

*No servers. No accounts. No metadata. Just chat.*
