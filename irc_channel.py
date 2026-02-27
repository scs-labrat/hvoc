"""IRC channel management over Veilid DHT.

Each channel is a DHT record with a shared-owner keypair (community write token).

Channel DHT record layout (veilid.DHTSchema.dflt(32)):
  subkey 0    : channel metadata
                {"name": "#general", "topic": "...", "modes": "nt",
                 "created": ts, "ops": [...], "bans": [...], "v": 2}
  subkeys 1-31: member slots
                {"nick": "alice", "route": "<base64>", "pk": "...",
                 "ts": <heartbeat>, "away": null|"brb"}

Share string format:  CHAN:<base64(json({"k": dht_key, "p": keypair}))>

Message types sent via app_message:
  msg, me, join, part, quit, nick, topic, notice,
  kick, invite, ping, pong, away, mode
"""

import asyncio
import base64
import fnmatch
import json
import time
import zlib

import veilid

from irc_log import get_logger

log = get_logger(__name__)

MAX_MEMBERS = 31          # subkeys 1..31
HEARTBEAT_INTERVAL = 30   # seconds between heartbeat writes
STALE_TIMEOUT = 90        # consider member gone after this


class ChannelMember:
    """Represents a discovered member in a channel."""
    __slots__ = ("nick", "route_blob", "route_id", "profile_key",
                 "last_seen", "subkey", "is_self", "away", "is_op")

    def __init__(self, nick, route_blob=None, route_id=None,
                 profile_key=None, last_seen=None, subkey=0,
                 is_self=False, away=None, is_op=False):
        self.nick = nick
        self.route_blob = route_blob
        self.route_id = route_id
        self.profile_key = profile_key
        self.last_seen = last_seen or time.time()
        self.subkey = subkey
        self.is_self = is_self
        self.away = away            # None or away message string
        self.is_op = is_op


class IRCChannel:
    """A single IRC channel backed by a Veilid DHT record."""

    def __init__(self, name):
        self.name = name
        self.topic = ""
        self.dht_key = None
        self.keypair = None
        self.my_subkey = None
        self.members: dict[str, ChannelMember] = {}
        self.messages: list[dict] = []
        self.unread = 0
        self.modes = set()          # channel modes: n, t, m, s, p, i
        self.ops: set[str] = set()  # nicks with operator status
        self.bans: list[str] = []   # banned nick patterns
        self._created = time.time()

    def add_message(self, msg: dict):
        self.messages.append(msg)
        if len(self.messages) > 2000:
            self.messages = self.messages[-1500:]

    def get_nicks(self) -> list[str]:
        """Sorted list of nicks, ops prefixed with @."""
        nicks = []
        for nick in sorted(self.members.keys(), key=str.lower):
            prefix = "@" if nick in self.ops else ""
            nicks.append(f"{prefix}{nick}")
        return nicks

    def get_raw_nicks(self) -> list[str]:
        """Sorted nicks without prefix."""
        return sorted(self.members.keys(), key=str.lower)


class ChannelManager:
    """Manages multiple IRC channels over Veilid."""

    def __init__(self, api, rc, my_route, nick, profile_key=None):
        self.api = api
        self.rc = rc
        self.my_route = my_route
        self.nick = nick
        self.profile_key = profile_key
        self.channels: dict[str, IRCChannel] = {}
        self.active_channel: str | None = None

        # Local state
        self.away_message: str | None = None
        self.ignore_list: set[str] = set()    # nicks to ignore
        self._ping_sent: dict[str, float] = {}  # nick -> send_time

        # Callbacks
        self.on_message = None
        self.on_member_join = None
        self.on_member_part = None
        self.on_status = None

        self._tasks: list[asyncio.Task] = []
        self._running = True

    # ------------------------------------------------------------------
    # Channel lifecycle
    # ------------------------------------------------------------------
    async def create_channel(self, name: str, topic: str = "",
                             modes: str = "nt") -> IRCChannel:
        name = _normalize_name(name)
        if name in self.channels:
            log.debug("Channel %s already exists locally", name)
            return self.channels[name]

        log.info("Creating channel %s (topic=%r, modes=%s)", name, topic, modes)
        ch = IRCChannel(name)
        ch.topic = topic
        ch.modes = set(modes)

        schema = veilid.DHTSchema.dflt(32)
        log.debug("Creating DHT record (32 subkeys)...")
        record = await self.rc.create_dht_record(
            veilid.CryptoKind.CRYPTO_KIND_VLD0, schema
        )
        ch.dht_key = record.key
        ch.keypair = record.owner_key_pair()
        log.info("DHT record created: %s", ch.dht_key)

        # Creator is an op
        ch.ops.add(self.nick)

        meta = {
            "name": name, "topic": topic,
            "modes": "".join(sorted(ch.modes)),
            "ops": list(ch.ops), "bans": ch.bans,
            "created": time.time(), "v": 2,
        }
        await rc_set(self.rc, ch.dht_key, 0, meta, ch.keypair)
        await self._claim_slot(ch)

        self.channels[name] = ch
        if self.active_channel is None:
            self.active_channel = name

        self._tasks.append(asyncio.create_task(self._poll_members_loop(ch)))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(ch)))

        self._notify(f"Created channel {name}")
        return ch

    async def join_channel(self, share_or_key: str,
                           name: str | None = None) -> IRCChannel:
        log.info("join_channel: share_or_key=%s...", share_or_key[:30])
        if share_or_key.upper().startswith("CHAN:"):
            payload = share_or_key[5:]
            info = json.loads(base64.b64decode(payload).decode())
            dht_key = veilid.RecordKey(info["k"])
            keypair = veilid.KeyPair(info["p"])
        else:
            raise ValueError(
                "Need a CHAN: share string (includes write keypair)."
            )

        log.debug("Opening DHT record: %s", dht_key)
        await self.rc.open_dht_record(dht_key, writer=keypair)

        log.debug("Reading channel metadata from subkey 0...")
        vd = await self.rc.get_dht_value(dht_key, veilid.ValueSubkey(0), True)
        meta = json.loads(vd.data.decode()) if vd else {}
        log.debug("Channel metadata: %s", meta)

        ch_name = name or meta.get("name", f"#{str(dht_key)[:8]}")
        ch_name = _normalize_name(ch_name)

        if ch_name in self.channels:
            log.debug("Already in channel %s", ch_name)
            return self.channels[ch_name]

        ch = IRCChannel(ch_name)
        ch.dht_key = dht_key
        ch.keypair = keypair
        ch.topic = meta.get("topic", "")
        ch.modes = set(meta.get("modes", "nt"))
        ch.ops = set(meta.get("ops", []))
        ch.bans = meta.get("bans", [])

        # Check if we're banned
        if _is_banned(self.nick, ch.bans):
            await self.rc.close_dht_record(dht_key)
            log.warning("Banned from %s", ch_name)
            raise RuntimeError(f"You are banned from {ch_name}")

        log.debug("Claiming slot in %s...", ch_name)
        await self._claim_slot(ch)

        self.channels[ch_name] = ch
        if self.active_channel is None:
            self.active_channel = ch_name

        self._tasks.append(asyncio.create_task(self._poll_members_loop(ch)))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(ch)))

        log.info("Joined channel %s (dht=%s)", ch_name, dht_key)
        self._notify(f"Joined {ch_name}")
        return ch

    async def part_channel(self, name: str):
        name = _normalize_name(name)
        ch = self.channels.get(name)
        if not ch:
            return

        if ch.my_subkey is not None:
            try:
                await rc_set(self.rc, ch.dht_key, ch.my_subkey,
                             {"nick": None}, ch.keypair)
            except Exception:
                pass

        try:
            await self.rc.close_dht_record(ch.dht_key)
        except Exception:
            pass

        for m in ch.members.values():
            if m.route_id and not m.is_self:
                try:
                    await self.api.release_private_route(m.route_id)
                except Exception:
                    pass

        del self.channels[name]
        if self.active_channel == name:
            self.active_channel = next(iter(self.channels), None)

    def get_share_string(self, name: str) -> str | None:
        ch = self.channels.get(_normalize_name(name))
        if not ch:
            return None
        info = {"k": str(ch.dht_key), "p": str(ch.keypair)}
        encoded = base64.b64encode(json.dumps(info).encode()).decode()
        return f"CHAN:{encoded}"

    # ------------------------------------------------------------------
    # Topic
    # ------------------------------------------------------------------
    async def set_topic(self, name: str, topic: str):
        ch = self.channels.get(_normalize_name(name))
        if not ch:
            return
        ch.topic = topic
        await self._write_metadata(ch)

    # ------------------------------------------------------------------
    # Modes
    # ------------------------------------------------------------------
    async def set_channel_mode(self, name: str, mode_str: str):
        """Apply mode changes like +mt or -s."""
        ch = self.channels.get(_normalize_name(name))
        if not ch:
            return

        adding = True
        for c in mode_str:
            if c == "+":
                adding = True
            elif c == "-":
                adding = False
            elif c in "ntmspi":
                if adding:
                    ch.modes.add(c)
                else:
                    ch.modes.discard(c)
        await self._write_metadata(ch)

    async def set_user_mode(self, ch_name: str, nick: str, mode_str: str):
        """Grant/revoke operator (+o) or voice (+v) on a nick."""
        ch = self.channels.get(_normalize_name(ch_name))
        if not ch:
            return
        adding = True
        for c in mode_str:
            if c == "+":
                adding = True
            elif c == "-":
                adding = False
            elif c == "o":
                if adding:
                    ch.ops.add(nick)
                else:
                    ch.ops.discard(nick)
        await self._write_metadata(ch)

    # ------------------------------------------------------------------
    # Bans
    # ------------------------------------------------------------------
    async def ban_user(self, ch_name: str, pattern: str):
        ch = self.channels.get(_normalize_name(ch_name))
        if not ch:
            return
        if pattern not in ch.bans:
            ch.bans.append(pattern)
        await self._write_metadata(ch)

    async def unban_user(self, ch_name: str, pattern: str):
        ch = self.channels.get(_normalize_name(ch_name))
        if not ch:
            return
        ch.bans = [b for b in ch.bans if b != pattern]
        await self._write_metadata(ch)

    async def kick_user(self, ch_name: str, nick: str, reason: str = ""):
        """Send a kick message. The kicked user's client should /part."""
        msg = {
            "t": "kick", "ch": _normalize_name(ch_name),
            "nick": nick, "by": self.nick,
            "reason": reason, "ts": time.time(),
        }
        await self.send_to_channel(ch_name, msg)

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    async def send_to_channel(self, name: str, msg: dict):
        ch = self.channels.get(_normalize_name(name))
        if not ch:
            return

        data = zlib.compress(json.dumps(msg, separators=(",", ":")).encode())
        sent = 0
        for member in list(ch.members.values()):
            if member.is_self or member.route_id is None:
                continue
            try:
                await self.rc.app_message(member.route_id, data)
                sent += 1
            except Exception as e:
                log.debug("Failed to send to %s in %s: %s", member.nick, name, e)
        log.debug("Sent %s to %d/%d members in %s",
                  msg.get("t", "?"), sent, len(ch.members), name)

    async def send_to_nick(self, nick: str, msg: dict):
        """Send a message to a specific nick across all shared channels."""
        data = zlib.compress(json.dumps(msg, separators=(",", ":")).encode())

        for ch in self.channels.values():
            m = ch.members.get(nick)
            if m and m.route_id and not m.is_self:
                try:
                    await self.rc.app_message(m.route_id, data)
                    log.debug("Sent %s to %s via %s", msg.get("t", "?"), nick, ch.name)
                except Exception as e:
                    log.debug("Failed to send to %s: %s", nick, e)
                return True
        log.debug("Nick %s not found in any channel", nick)
        return False

    async def send_chat(self, channel_name: str, text: str):
        msg = {
            "t": "msg", "ch": _normalize_name(channel_name),
            "from": self.nick, "text": text, "ts": time.time(),
        }
        await self.send_to_channel(channel_name, msg)

    async def send_action(self, channel_name: str, text: str):
        msg = {
            "t": "me", "ch": _normalize_name(channel_name),
            "from": self.nick, "text": text, "ts": time.time(),
        }
        await self.send_to_channel(channel_name, msg)

    async def send_notice(self, target: str, text: str):
        """Send a NOTICE to a nick or channel."""
        msg = {
            "t": "notice", "from": self.nick,
            "text": text, "ts": time.time(),
        }
        if target.startswith("#"):
            msg["ch"] = _normalize_name(target)
            await self.send_to_channel(target, msg)
        else:
            msg["ch"] = ""  # DM notice
            await self.send_to_nick(target, msg)

    async def send_join_notice(self, ch: IRCChannel):
        msg = {
            "t": "join", "ch": ch.name,
            "nick": self.nick, "ts": time.time(),
        }
        await self.send_to_channel(ch.name, msg)

    async def send_part_notice(self, channel_name: str, reason: str = ""):
        msg = {
            "t": "part", "ch": _normalize_name(channel_name),
            "nick": self.nick, "reason": reason, "ts": time.time(),
        }
        await self.send_to_channel(channel_name, msg)

    async def send_nick_change(self, old_nick: str, new_nick: str):
        msg = {
            "t": "nick", "old": old_nick,
            "new": new_nick, "ts": time.time(),
        }
        for ch_name in list(self.channels):
            await self.send_to_channel(ch_name, msg)

    async def send_quit_notice(self, reason: str = ""):
        msg = {
            "t": "quit", "nick": self.nick,
            "reason": reason, "ts": time.time(),
        }
        for ch_name in list(self.channels):
            await self.send_to_channel(ch_name, msg)

    async def send_away_notice(self):
        """Broadcast away status to all channels."""
        msg = {
            "t": "away", "nick": self.nick,
            "message": self.away_message, "ts": time.time(),
        }
        for ch_name in list(self.channels):
            await self.send_to_channel(ch_name, msg)

    async def send_invite(self, ch_name: str, nick: str):
        """Send an invite message with the share string."""
        share = self.get_share_string(ch_name)
        msg = {
            "t": "invite", "ch": _normalize_name(ch_name),
            "from": self.nick, "to": nick,
            "share": share, "ts": time.time(),
        }
        await self.send_to_nick(nick, msg)

    async def send_ping(self, nick: str) -> bool:
        """Send a PING to measure RTT. Returns True if nick found."""
        self._ping_sent[nick] = time.time()
        msg = {
            "t": "ping", "from": self.nick, "ts": time.time(),
        }
        return await self.send_to_nick(nick, msg)

    async def send_pong(self, nick: str, orig_ts: float):
        """Reply to a PING."""
        msg = {
            "t": "pong", "from": self.nick,
            "orig_ts": orig_ts, "ts": time.time(),
        }
        await self.send_to_nick(nick, msg)

    # ------------------------------------------------------------------
    # Incoming message dispatch
    # ------------------------------------------------------------------
    def dispatch_message(self, raw_msg: dict):
        kind = raw_msg.get("t")
        ch_name = raw_msg.get("ch")
        sender = raw_msg.get("from") or raw_msg.get("nick")
        log.debug("dispatch_message: kind=%s from=%s ch=%s", kind, sender, ch_name)

        # Ignore list check
        if sender and sender.lower() in {n.lower() for n in self.ignore_list}:
            return

        # Nick changes are global
        if kind == "nick":
            old = raw_msg.get("old")
            new = raw_msg.get("new")
            for ch in self.channels.values():
                if old in ch.members:
                    member = ch.members.pop(old)
                    member.nick = new
                    ch.members[new] = member
                    if old in ch.ops:
                        ch.ops.discard(old)
                        ch.ops.add(new)
                    ch.add_message(raw_msg)
                    if self.on_message:
                        self.on_message(ch.name, raw_msg)
            return

        if kind == "quit":
            nick = raw_msg.get("nick")
            for ch in self.channels.values():
                if nick in ch.members:
                    del ch.members[nick]
                    ch.add_message(raw_msg)
                    if self.on_message:
                        self.on_message(ch.name, raw_msg)
            return

        # Ping/pong are direct
        if kind == "ping":
            from_nick = raw_msg.get("from")
            orig_ts = raw_msg.get("ts", 0)
            if from_nick:
                asyncio.create_task(self.send_pong(from_nick, orig_ts))
            return

        if kind == "pong":
            from_nick = raw_msg.get("from")
            orig_ts = raw_msg.get("orig_ts", 0)
            if from_nick and from_nick in self._ping_sent:
                rtt = time.time() - self._ping_sent.pop(from_nick)
                pong_msg = {
                    "t": "sys", "text": f"PONG from {from_nick}: {rtt*1000:.0f}ms",
                    "ts": time.time(),
                }
                # Show in active channel
                if self.active_channel and self.active_channel in self.channels:
                    ch = self.channels[self.active_channel]
                    ch.add_message(pong_msg)
                    if self.on_message:
                        self.on_message(self.active_channel, pong_msg)
            return

        # Away notifications
        if kind == "away":
            nick = raw_msg.get("nick")
            away_msg = raw_msg.get("message")
            for ch in self.channels.values():
                if nick in ch.members:
                    ch.members[nick].away = away_msg
                    ch.add_message(raw_msg)
                    if self.on_message:
                        self.on_message(ch.name, raw_msg)
            return

        # Invite (DM)
        if kind == "invite":
            to_nick = raw_msg.get("to")
            if to_nick and to_nick.lower() == self.nick.lower():
                # Show invite in active channel
                if self.active_channel and self.active_channel in self.channels:
                    inv_ch = raw_msg.get("ch", "?")
                    from_nick = raw_msg.get("from", "?")
                    share = raw_msg.get("share", "")
                    inv_msg = {
                        "t": "sys",
                        "text": (f"{from_nick} invited you to {inv_ch}. "
                                 f"Use: /join {share}"),
                        "ts": time.time(),
                    }
                    ch = self.channels[self.active_channel]
                    ch.add_message(inv_msg)
                    if self.on_message:
                        self.on_message(self.active_channel, inv_msg)
            return

        # Notice (can be channel or DM)
        if kind == "notice":
            if ch_name and ch_name in self.channels:
                ch = self.channels[ch_name]
                ch.add_message(raw_msg)
                if self.on_message:
                    self.on_message(ch_name, raw_msg)
            elif self.active_channel and self.active_channel in self.channels:
                ch = self.channels[self.active_channel]
                ch.add_message(raw_msg)
                if self.on_message:
                    self.on_message(self.active_channel, raw_msg)
            return

        if not ch_name or ch_name not in self.channels:
            return

        ch = self.channels[ch_name]

        if kind == "join":
            nick = raw_msg.get("nick")
            if nick and nick not in ch.members:
                ch.members[nick] = ChannelMember(nick, last_seen=time.time())
            ch.add_message(raw_msg)
            if self.on_member_join:
                self.on_member_join(ch_name, nick)
            if self.on_message:
                self.on_message(ch_name, raw_msg)

        elif kind == "part":
            nick = raw_msg.get("nick")
            if nick in ch.members:
                del ch.members[nick]
            ch.add_message(raw_msg)
            if self.on_member_part:
                self.on_member_part(ch_name, nick)
            if self.on_message:
                self.on_message(ch_name, raw_msg)

        elif kind == "kick":
            nick = raw_msg.get("nick")
            by = raw_msg.get("by", "???")
            reason = raw_msg.get("reason", "")
            ch.add_message(raw_msg)
            if self.on_message:
                self.on_message(ch_name, raw_msg)
            # If we're kicked, auto-part
            if nick and nick.lower() == self.nick.lower():
                asyncio.create_task(self._handle_kicked(ch_name, by, reason))

        elif kind in ("msg", "me", "topic"):
            ch.add_message(raw_msg)
            if kind == "topic":
                ch.topic = raw_msg.get("text", "")
            if self.on_message:
                self.on_message(ch_name, raw_msg)

        elif kind == "mode":
            ch.add_message(raw_msg)
            mode_str = raw_msg.get("mode", "")
            # Apply modes
            for c in mode_str.lstrip("+-"):
                pass  # display only; metadata is source of truth
            if self.on_message:
                self.on_message(ch_name, raw_msg)

    async def _handle_kicked(self, ch_name, by, reason):
        """Auto-part after being kicked."""
        try:
            await self.part_channel(ch_name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Whois
    # ------------------------------------------------------------------
    def whois(self, nick: str) -> dict | None:
        """Get info about a nick from local state."""
        info = {"nick": nick, "channels": [], "away": None}
        for ch_name, ch in self.channels.items():
            if nick in ch.members:
                m = ch.members[nick]
                prefix = "@" if nick in ch.ops else ""
                info["channels"].append(f"{prefix}{ch_name}")
                info["away"] = m.away
                info["last_seen"] = m.last_seen
                info["profile_key"] = m.profile_key
        return info if info["channels"] else None

    # ------------------------------------------------------------------
    # Internal: slot management
    # ------------------------------------------------------------------
    async def _claim_slot(self, ch: IRCChannel):
        log.debug("Claiming slot in %s...", ch.name)
        route_b64 = base64.b64encode(self.my_route.blob).decode()
        entry = {
            "nick": self.nick,
            "route": route_b64,
            "pk": str(self.profile_key) if self.profile_key else None,
            "ts": time.time(),
            "away": self.away_message,
        }

        for subkey in range(1, MAX_MEMBERS + 1):
            log.debug("  Checking subkey %d in %s", subkey, ch.name)
            vd = await self.rc.get_dht_value(
                ch.dht_key, veilid.ValueSubkey(subkey), True
            )

            if vd is None or vd.data == b"" or vd.data == b"{}":
                log.info("  Claimed empty subkey %d in %s", subkey, ch.name)
                await rc_set(self.rc, ch.dht_key, subkey, entry, ch.keypair)
                ch.my_subkey = subkey
                ch.members[self.nick] = ChannelMember(
                    self.nick, is_self=True, subkey=subkey,
                    is_op=(self.nick in ch.ops),
                )
                return

            try:
                existing = json.loads(vd.data.decode())
                if not existing.get("nick"):
                    log.info("  Claimed cleared subkey %d in %s", subkey, ch.name)
                    await rc_set(self.rc, ch.dht_key, subkey, entry, ch.keypair)
                    ch.my_subkey = subkey
                    ch.members[self.nick] = ChannelMember(
                        self.nick, is_self=True, subkey=subkey,
                        is_op=(self.nick in ch.ops),
                    )
                    return

                ts = existing.get("ts", 0)
                if time.time() - ts > STALE_TIMEOUT * 3:
                    log.info("  Claimed stale subkey %d in %s (nick=%s, age=%.0fs)",
                             subkey, ch.name, existing.get("nick"), time.time() - ts)
                    await rc_set(self.rc, ch.dht_key, subkey, entry, ch.keypair)
                    ch.my_subkey = subkey
                    ch.members[self.nick] = ChannelMember(
                        self.nick, is_self=True, subkey=subkey,
                        is_op=(self.nick in ch.ops),
                    )
                    return
                log.debug("  Subkey %d occupied by %s", subkey, existing.get("nick"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.info("  Claimed corrupted subkey %d in %s", subkey, ch.name)
                await rc_set(self.rc, ch.dht_key, subkey, entry, ch.keypair)
                ch.my_subkey = subkey
                ch.members[self.nick] = ChannelMember(
                    self.nick, is_self=True, subkey=subkey,
                    is_op=(self.nick in ch.ops),
                )
                return

        log.error("Channel %s is full (%d members)", ch.name, MAX_MEMBERS)
        raise RuntimeError(f"Channel {ch.name} is full ({MAX_MEMBERS} members)")

    # ------------------------------------------------------------------
    # Internal: background tasks
    # ------------------------------------------------------------------
    async def _poll_members_loop(self, ch: IRCChannel):
        log.debug("Starting poll loop for %s", ch.name)
        while self._running and ch.name in self.channels:
            try:
                await self._scan_members(ch)
            except Exception as e:
                log.debug("Poll error in %s: %s", ch.name, e)
            await asyncio.sleep(5)
        log.debug("Poll loop ended for %s", ch.name)

    async def _scan_members(self, ch: IRCChannel):
        now = time.time()
        seen_nicks = set()

        for subkey in range(1, MAX_MEMBERS + 1):
            try:
                vd = await self.rc.get_dht_value(
                    ch.dht_key, veilid.ValueSubkey(subkey), True
                )
                if vd is None:
                    continue
                entry = json.loads(vd.data.decode())
                nick = entry.get("nick")
                if not nick:
                    continue

                ts = entry.get("ts", 0)
                if now - ts > STALE_TIMEOUT:
                    continue

                seen_nicks.add(nick)

                if nick == self.nick:
                    continue

                if nick not in ch.members:
                    route_b64 = entry.get("route")
                    if route_b64:
                        try:
                            blob = base64.b64decode(route_b64)
                            route_id = await self.api.import_remote_private_route(blob)
                            ch.members[nick] = ChannelMember(
                                nick=nick, route_blob=blob,
                                route_id=route_id,
                                profile_key=entry.get("pk"),
                                last_seen=ts, subkey=subkey,
                                away=entry.get("away"),
                            )
                            log.info("Discovered member %s in %s (subkey %d)",
                                     nick, ch.name, subkey)
                            if self.on_member_join:
                                self.on_member_join(ch.name, nick)
                            join_msg = {
                                "t": "join", "ch": ch.name,
                                "nick": nick, "ts": ts,
                            }
                            ch.add_message(join_msg)
                            if self.on_message:
                                self.on_message(ch.name, join_msg)
                        except Exception as e:
                            log.debug("Failed to import route for %s: %s", nick, e)
                else:
                    ch.members[nick].last_seen = ts
                    ch.members[nick].away = entry.get("away")

            except Exception:
                continue

        # Prune stale
        for nick in list(ch.members):
            m = ch.members[nick]
            if m.is_self:
                continue
            if nick not in seen_nicks and now - m.last_seen > STALE_TIMEOUT:
                log.info("Pruning stale member %s from %s", nick, ch.name)
                if m.route_id:
                    try:
                        await self.api.release_private_route(m.route_id)
                    except Exception:
                        pass
                del ch.members[nick]
                if self.on_member_part:
                    self.on_member_part(ch.name, nick)
                part_msg = {
                    "t": "part", "ch": ch.name,
                    "nick": nick, "ts": now,
                }
                ch.add_message(part_msg)
                if self.on_message:
                    self.on_message(ch.name, part_msg)

    async def _heartbeat_loop(self, ch: IRCChannel):
        log.debug("Starting heartbeat loop for %s", ch.name)
        while self._running and ch.name in self.channels:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if ch.my_subkey is None:
                continue
            try:
                route_b64 = base64.b64encode(self.my_route.blob).decode()
                entry = {
                    "nick": self.nick,
                    "route": route_b64,
                    "pk": str(self.profile_key) if self.profile_key else None,
                    "ts": time.time(),
                    "away": self.away_message,
                }
                await rc_set(self.rc, ch.dht_key, ch.my_subkey, entry, ch.keypair)
            except Exception as e:
                log.debug("Heartbeat write error in %s: %s", ch.name, e)
        log.debug("Heartbeat loop ended for %s", ch.name)

    async def _write_metadata(self, ch: IRCChannel):
        """Persist channel metadata to DHT subkey 0."""
        log.debug("Writing metadata for %s", ch.name)
        meta = {
            "name": ch.name, "topic": ch.topic,
            "modes": "".join(sorted(ch.modes)),
            "ops": list(ch.ops), "bans": ch.bans,
            "created": ch._created, "v": 2,
        }
        await rc_set(self.rc, ch.dht_key, 0, meta, ch.keypair)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    async def shutdown(self):
        log.info("ChannelManager.shutdown() — stopping...")
        self._running = False
        try:
            await self.send_quit_notice()
        except Exception as e:
            log.debug("Quit notice error: %s", e)

        for ch_name in list(self.channels):
            try:
                log.debug("Parting channel %s...", ch_name)
                await self.part_channel(ch_name)
            except Exception as e:
                log.debug("Part error for %s: %s", ch_name, e)

        log.debug("Cancelling %d background tasks...", len(self._tasks))
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        log.info("ChannelManager shutdown complete")

    def _notify(self, text: str):
        if self.on_status:
            self.on_status(text)


# ======================================================================
# Helpers
# ======================================================================

def _normalize_name(name: str) -> str:
    name = name.strip().lower()
    if not name.startswith("#"):
        name = "#" + name
    return name


def _is_banned(nick: str, bans: list[str]) -> bool:
    """Check if a nick matches any ban pattern."""
    nick_lower = nick.lower()
    for pattern in bans:
        if pattern.lower() == nick_lower:
            return True
        if "*" in pattern:
            if fnmatch.fnmatch(nick_lower, pattern.lower()):
                return True
    return False


async def rc_set(rc, dht_key, subkey: int, data: dict, keypair):
    opts = veilid.SetDHTValueOptions(writer=keypair)
    await rc.set_dht_value(
        dht_key,
        veilid.ValueSubkey(subkey),
        json.dumps(data).encode(),
        options=opts,
    )
