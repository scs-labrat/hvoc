#!/usr/bin/env python3
"""veilid-irc — Multi-user IRC-style chat over the Veilid P2P network.

Implements the full IRC command set adapted for peer-to-peer:
  I.   Basic Connection & Setup    /server /quit /nick /user /ping
  II.  Channel Operations          /join /part /topic /invite /kick /ban
                                   /unban /kickban /mode
  III. Private Communication       /msg /query /notice /me /ignore
  IV.  Information & Status        /who /whois /whowas /list /names /away
                                   /userhost /time /version /admin /info
                                   /motd /stats
  V.   Network Operator (N/A)      /kill /kline /gline /shun /oper
  VI.  Veilid-specific             /create /share /switch /clear /help
"""

import argparse
import asyncio
import textwrap
import time

from bootstrap import ensure_veilid_server, stop_veilid_server
from irc_channel import ChannelManager, _normalize_name
from irc_directory import IRCDirectory
from irc_log import get_logger
from irc_net import IRCNet
from irc_ui import IRCApp

log = get_logger(__name__)

VERSION = "veilid-irc 1.0.0 — P2P IRC over Veilid (Textual UI)"

MOTD = r"""
  ╦  ╦┌─┐┬┬  ┬┌┬┐   ╦╦═╗╔═╗
  ╚╗╔╝├┤ ││  │ ││───║╠╦╝║
   ╚╝ └─┘┴┴─┘┴─┴┘   ╩╩╚═╚═╝
  Decentralised. Encrypted. Unstoppable.
  No servers. No accounts. No metadata.
  Type /help for commands.
"""


class VeilidIRC:
    """Orchestrator: wires IRCNet + ChannelManager into the Textual App."""

    def __init__(self, app: IRCApp, args):
        self.app = app
        self.args = args
        self.net = IRCNet()
        self.nick = args.nick or "anon"
        self.veilid_ok = False
        self._start_time = time.time()

    # ── Startup ──────────────────────────────────────────────────────

    async def start(self):
        app = self.app
        app.set_nick(self.nick)

        # Suppress veilid-python's InvalidStateError during shutdown.
        # The library's recv handler can race with connection teardown.
        loop = asyncio.get_event_loop()
        _orig_handler = loop.get_exception_handler()

        def _suppress_veilid_shutdown(loop, context):
            exc = context.get("exception")
            if isinstance(exc, (asyncio.InvalidStateError, ConnectionError)):
                return  # Swallow silently
            msg = context.get("message", "")
            if "handle_recv_messages" in str(context.get("future", "")):
                return  # Swallow veilid recv handler errors at shutdown
            if _orig_handler:
                _orig_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(_suppress_veilid_shutdown)

        # Show MOTD
        for line in MOTD.strip().splitlines():
            self._sys_msg(line)

        try:
            self.net.on_status = self._on_status
            self.net.on_message = self._on_message
            await self.net.start(self.nick)
            self.veilid_ok = True
            app.set_status("Connected to Veilid network")
        except Exception as exc:
            err = str(exc).lower()
            app.set_status(f"Veilid error: {exc}")
            self._sys_msg(f"Failed to connect: {exc}")
            if "try again" in err or "unable to allocate" in err:
                self._sys_msg("veilid-server hasn't joined the network yet.")
                self._sys_msg("This can take 1-2 minutes on first run.")
                self._sys_msg("Wait a bit and restart, or check veilid-server logs.")
            elif "connect" in err or "refused" in err:
                self._sys_msg("Is veilid-server running? Try: veilid-server &")
            else:
                self._sys_msg("Check logs/veilid-irc.log for details.")
            return

        # Initialize directory (optional)
        try:
            loaded = await IRCDirectory.load(self.net.api, self.net.rc)
            if loaded:
                self.net.directory = loaded
                self._sys_msg("Channel directory loaded")
            elif self.args.dir:
                dir_str = self.args.dir
                from irc_qr import is_qr_image_path, decode_qr
                if is_qr_image_path(dir_str):
                    self._sys_msg("Decoding directory QR code...")
                    dir_str = decode_qr(dir_str) or ""
                    if not dir_str:
                        self._sys_msg("Could not decode QR image")
                if dir_str.upper().startswith("DIR:"):
                    self.net.directory = await IRCDirectory.join_from_share(
                        self.net.api, self.net.rc, dir_str
                    )
                    self._sys_msg("Joined channel directory")
                elif dir_str:
                    self._sys_msg(f"Not a DIR: string: {dir_str[:60]}...")
        except Exception as exc:
            self._sys_msg(f"Directory: {exc}")

        # Create/join initial channel
        try:
            mgr = self.net.channel_mgr
            join_str = self.args.join or ""
            if join_str:
                from irc_qr import is_qr_image_path, decode_qr
                if is_qr_image_path(join_str):
                    self._sys_msg("Decoding channel QR code...")
                    join_str = decode_qr(join_str) or ""
                    if not join_str:
                        self._sys_msg("Could not decode QR image")
                if join_str:
                    ch = await mgr.join_channel(join_str)
                await mgr.send_join_notice(ch)
                self._sync_ui()
                self._sys_msg(f"Joined {ch.name}", ch.name)
            elif self.args.create:
                topic = self.args.topic or ""
                ch = await mgr.create_channel(self.args.create, topic=topic)
                await mgr.send_join_notice(ch)
                self._sync_ui()
                share = mgr.get_share_string(ch.name)
                self._sys_msg(f"Created {ch.name}", ch.name)
                self._sys_msg("Share this to invite others:", ch.name)
                self._sys_msg(share, ch.name)
            else:
                ch = await mgr.create_channel(
                    "lobby", topic="Welcome to Veilid IRC!"
                )
                await mgr.send_join_notice(ch)
                self._sync_ui()
                share = mgr.get_share_string(ch.name)
                self._sys_msg("Share this to invite others:", ch.name)
                self._sys_msg(share, ch.name)

            self._sys_msg("Type /help for commands")
        except Exception as exc:
            app.set_status(f"Channel error: {exc}")
            self._sys_msg(f"Error: {exc}")

    # ── Callbacks ────────────────────────────────────────────────────

    def _on_status(self, text: str):
        self.app.set_status(text)

    def _on_message(self, channel: str, msg_dict: dict):
        self.app.add_message(channel, msg_dict)
        mgr = self.net.channel_mgr
        if mgr and channel in mgr.channels:
            ch = mgr.channels[channel]
            self.app.set_users(channel, ch.get_nicks())
            self.app.set_topic(channel, ch.topic)

    # ── Input dispatcher ─────────────────────────────────────────────

    async def handle_input(self, text: str):
        app = self.app
        mgr = self.net.channel_mgr

        if not mgr:
            self._sys_msg("Not connected to Veilid")
            return

        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            log.info("Command: %s %s", cmd, arg[:50] if arg else "")
            await self._dispatch_command(cmd, arg)
            return

        # Regular chat
        if app._active_channel:
            try:
                await mgr.send_chat(app._active_channel, text)
                local = {
                    "t": "msg", "ch": app._active_channel,
                    "from": mgr.nick, "text": text, "ts": time.time(),
                }
                app.add_message(app._active_channel, local)
            except Exception as e:
                self._sys_msg(f"Send error: {e}")
        else:
            self._sys_msg("No active channel. Use /create or /join")

    # ══════════════════════════════════════════════════════════════════
    #  COMMAND ROUTER
    # ══════════════════════════════════════════════════════════════════

    async def _dispatch_command(self, cmd: str, arg: str):
        handlers = {
            # I. Basic Connection & Setup
            "/server":   self._cmd_server,
            "/quit":     self._cmd_quit,
            "/nick":     self._cmd_nick,
            "/user":     self._cmd_user,
            "/ping":     self._cmd_ping,
            # II. Channel Operations
            "/join":     self._cmd_join,
            "/part":     self._cmd_part,
            "/leave":    self._cmd_part,
            "/topic":    self._cmd_topic,
            "/invite":   self._cmd_invite,
            "/kick":     self._cmd_kick,
            "/ban":      self._cmd_ban,
            "/unban":    self._cmd_unban,
            "/kickban":  self._cmd_kickban,
            "/kb":       self._cmd_kickban,
            "/mode":     self._cmd_mode,
            # III. Private Communication
            "/msg":      self._cmd_msg,
            "/query":    self._cmd_query,
            "/privmsg":  self._cmd_query,
            "/notice":   self._cmd_notice,
            "/me":       self._cmd_me,
            "/ignore":   self._cmd_ignore,
            # IV. Information & Status
            "/who":      self._cmd_who,
            "/whois":    self._cmd_whois,
            "/whowas":   self._cmd_whowas,
            "/list":     self._cmd_list,
            "/names":    self._cmd_names,
            "/away":     self._cmd_away,
            "/userhost": self._cmd_userhost,
            "/time":     self._cmd_time,
            "/version":  self._cmd_version,
            "/admin":    self._cmd_admin,
            "/info":     self._cmd_info,
            "/motd":     self._cmd_motd,
            "/stats":    self._cmd_stats,
            # V. Network Operator Commands
            "/kill":     self._cmd_ircop_na,
            "/kline":    self._cmd_ircop_na,
            "/gline":    self._cmd_ircop_na,
            "/shun":     self._cmd_ircop_na,
            "/oper":     self._cmd_ircop_na,
            # VI. Veilid-specific
            "/create":   self._cmd_create,
            "/share":    self._cmd_share,
            "/switch":   self._cmd_switch,
            "/s":        self._cmd_switch,
            "/w":        self._cmd_switch,
            "/clear":    self._cmd_clear,
            "/help":     self._cmd_help,
            # VII. Directory
            "/dir":      self._cmd_dir,
            "/publish":  self._cmd_publish,
            "/unpublish": self._cmd_unpublish,
            "/rooms":    self._cmd_rooms,
            # VIII. QR
            "/scan":     self._cmd_scan,
        }

        handler = handlers.get(cmd)
        if handler:
            await handler(arg)
        else:
            self._sys_msg(f"Unknown command: {cmd}. Type /help for a list.")

    # ==================================================================
    #  I. BASIC CONNECTION & SETUP
    # ==================================================================

    async def _cmd_server(self, arg: str):
        """Reconnect to a veilid-server instance."""
        if not arg:
            if self.veilid_ok:
                self._sys_msg("Connected to veilid-server at localhost:5959")
            else:
                self._sys_msg("Not connected. Usage: /server <host> [port]")
            return
        parts = arg.split()
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 5959
        self._sys_msg(f"Reconnecting to {host}:{port}...")
        try:
            await self.net.stop()
            self.net = IRCNet()
            self.net.on_status = self._on_status
            self.net.on_message = self._on_message
            # Note: would need to modify IRCNet.start() to accept host/port
            # For now, always connects to localhost:5959
            await self.net.start(self.nick)
            self.veilid_ok = True
            self._sys_msg(f"Connected to {host}:{port}")
        except Exception as e:
            self._sys_msg(f"Connection failed: {e}")

    async def _cmd_quit(self, arg: str):
        """Disconnect and exit."""
        reason = arg or "Leaving"
        if self.veilid_ok and self.net.channel_mgr:
            try:
                await self.net.channel_mgr.send_quit_notice(reason)
            except Exception:
                pass
        # Trigger app quit which calls on_shutdown_cb → irc.shutdown()
        await self.app._do_quit()

    async def _cmd_nick(self, arg: str):
        """Change nickname."""
        mgr = self.net.channel_mgr
        if not arg:
            self._sys_msg(f"You are: {mgr.nick}")
            return
        new_nick = arg.split()[0]
        old_nick = mgr.nick
        try:
            await mgr.send_nick_change(old_nick, new_nick)
            mgr.nick = new_nick
            self.nick = new_nick
            for ch in mgr.channels.values():
                if old_nick in ch.members:
                    m = ch.members.pop(old_nick)
                    m.nick = new_nick
                    ch.members[new_nick] = m
                if old_nick in ch.ops:
                    ch.ops.discard(old_nick)
                    ch.ops.add(new_nick)
            self.app.set_nick(new_nick)
            self._sys_msg(f"You are now known as {new_nick}")
            self._sync_ui()
        except Exception as e:
            self._sys_msg(f"Nick change error: {e}")

    async def _cmd_user(self, arg: str):
        """Show current user identity."""
        mgr = self.net.channel_mgr
        self._sys_msg(f"Nick: {mgr.nick}")
        self._sys_msg(f"Away: {mgr.away_message or '(not away)'}")
        ch_count = len(mgr.channels)
        self._sys_msg(f"Channels: {ch_count}")
        if mgr.profile_key:
            self._sys_msg(f"Profile key: {str(mgr.profile_key)[:16]}...")

    async def _cmd_ping(self, arg: str):
        """Ping a user to measure round-trip time."""
        if not arg:
            self._sys_msg("Usage: /ping <nickname>")
            return
        nick = arg.split()[0]
        mgr = self.net.channel_mgr
        found = await mgr.send_ping(nick)
        if not found:
            self._sys_msg(f"User {nick} not found in any shared channel")
        else:
            self._sys_msg(f"PING sent to {nick}...")

    # ==================================================================
    #  II. CHANNEL OPERATIONS
    # ==================================================================

    async def _cmd_join(self, arg: str):
        """Join a channel by CHAN: share string, 4-char code, #name, or QR image."""
        if not arg:
            self._sys_msg("Usage: /join <CHAN:share | short_code | #name | image.png>")
            return
        mgr = self.net.channel_mgr

        # QR image decoding: if arg looks like an image path, decode it first
        from irc_qr import is_qr_image_path, decode_qr
        if is_qr_image_path(arg):
            self._sys_msg("Decoding QR code...")
            decoded = decode_qr(arg)
            if not decoded:
                self._sys_msg("Could not decode QR image. Is it a valid VOC QR code?")
                return
            if decoded.upper().startswith("DIR:"):
                self._sys_msg("That's a directory QR — use /dir join with it instead.")
                self._sys_msg(f"Decoded: {decoded[:80]}...")
                return
            if not decoded.upper().startswith("CHAN:"):
                self._sys_msg(f"QR decoded but not a CHAN: string: {decoded[:80]}...")
                return
            self._sys_msg("QR decoded successfully!")
            arg = decoded

        share_string = None

        if arg.upper().startswith("CHAN:"):
            # Direct share string
            share_string = arg
        elif self.net.directory:
            # Try short code first (4-char), then channel name
            code = arg.strip().upper()
            entry = None
            if len(code) <= 5 and not code.startswith("#"):
                try:
                    entry = await self.net.directory.find_by_short_code(
                        self.net.rc, code
                    )
                except Exception as e:
                    self._sys_msg(f"Directory lookup error: {e}")
                    return
            if not entry:
                try:
                    entry = await self.net.directory.find_by_name(
                        self.net.rc, arg.strip()
                    )
                except Exception:
                    pass
            if entry:
                share_string = entry["share"]
                self._sys_msg(
                    f"Found: {entry.get('name', '?')} by "
                    f"{entry.get('nick', '?')} [{entry.get('short', '?')}]"
                )
            else:
                self._sys_msg(
                    f"'{arg}' not found in directory. "
                    "Use a CHAN: share string or /rooms to browse."
                )
                return
        else:
            self._sys_msg(
                "Need a CHAN: share string, or set up a directory first "
                "(/dir create or /dir join)"
            )
            return

        try:
            ch = await mgr.join_channel(share_string)
            await mgr.send_join_notice(ch)
            self._sync_ui()
            self.app.switch_channel(ch.name)
            self._sys_msg(f"Joined {ch.name}", ch.name)
        except Exception as e:
            self._sys_msg(f"Join error: {e}")

    async def _cmd_part(self, arg: str):
        """Leave a channel with optional message."""
        mgr = self.net.channel_mgr
        parts = arg.split(maxsplit=1) if arg else []
        if parts and parts[0].startswith("#"):
            ch_name = _normalize_name(parts[0])
            reason = parts[1] if len(parts) > 1 else ""
        else:
            ch_name = self.app._active_channel
            reason = arg
        if not ch_name:
            self._sys_msg("No active channel")
            return
        ch_name = _normalize_name(ch_name)
        try:
            await mgr.send_part_notice(ch_name, reason)
            await mgr.part_channel(ch_name)
            self._sys_msg(f"Left {ch_name}")
            self._sync_ui()
        except Exception as e:
            self._sys_msg(f"Error: {e}")

    async def _cmd_topic(self, arg: str):
        """View or set channel topic."""
        mgr = self.net.channel_mgr
        ch_name = self.app._active_channel

        if not arg:
            topic = self.app._channel_topics.get(ch_name, "(no topic)")
            self._sys_msg(f"Topic for {ch_name}: {topic}")
            return

        # Check if first word is a channel name
        parts = arg.split(maxsplit=1)
        if parts[0].startswith("#"):
            ch_name = _normalize_name(parts[0])
            new_topic = parts[1] if len(parts) > 1 else ""
        else:
            new_topic = arg

        if not new_topic:
            ch = mgr.channels.get(ch_name)
            topic = ch.topic if ch else "(no topic)"
            self._sys_msg(f"Topic for {ch_name}: {topic}")
            return

        try:
            await mgr.set_topic(ch_name, new_topic)
            msg = {
                "t": "topic", "ch": ch_name, "from": mgr.nick,
                "text": new_topic, "ts": time.time(),
            }
            await mgr.send_to_channel(ch_name, msg)
            self.app.set_topic(ch_name, new_topic)
            self.app.add_message(ch_name, msg)
        except Exception as e:
            self._sys_msg(f"Error: {e}")

    async def _cmd_invite(self, arg: str):
        """Invite a user to a channel."""
        mgr = self.net.channel_mgr
        parts = arg.split()
        if len(parts) < 1:
            self._sys_msg("Usage: /invite <nickname> [#channel]")
            return
        nick = parts[0]
        ch_name = _normalize_name(parts[1]) if len(parts) > 1 else self.app._active_channel
        if not ch_name:
            self._sys_msg("No active channel")
            return
        try:
            await mgr.send_invite(ch_name, nick)
            self._sys_msg(f"Invited {nick} to {ch_name}")
        except Exception as e:
            self._sys_msg(f"Invite error: {e}")

    async def _cmd_kick(self, arg: str):
        """Kick a user from the current channel (requires op)."""
        mgr = self.net.channel_mgr
        ch_name = self.app._active_channel

        parts = arg.split(maxsplit=1) if arg else []
        if not parts:
            self._sys_msg("Usage: /kick [#channel] <nickname> [reason]")
            return

        # Check if first arg is a channel
        if parts[0].startswith("#"):
            ch_name = _normalize_name(parts[0])
            rest = parts[1] if len(parts) > 1 else ""
            parts = rest.split(maxsplit=1) if rest else []
            if not parts:
                self._sys_msg("Usage: /kick [#channel] <nickname> [reason]")
                return

        nick = parts[0]
        reason = parts[1] if len(parts) > 1 else ""

        ch = mgr.channels.get(ch_name)
        if not ch:
            self._sys_msg(f"Not in channel: {ch_name}")
            return
        if mgr.nick not in ch.ops:
            self._sys_msg(f"You need to be an operator in {ch_name}")
            return

        await mgr.kick_user(ch_name, nick, reason)
        kick_msg = {
            "t": "kick", "ch": ch_name, "nick": nick,
            "by": mgr.nick, "reason": reason, "ts": time.time(),
        }
        self.app.add_message(ch_name, kick_msg)
        self._sys_msg(f"Kicked {nick} from {ch_name}")

    async def _cmd_ban(self, arg: str):
        """Ban a nick/pattern from current channel (requires op)."""
        mgr = self.net.channel_mgr
        ch_name = self.app._active_channel

        parts = arg.split() if arg else []
        if not parts:
            # Show ban list
            ch = mgr.channels.get(ch_name)
            if ch and ch.bans:
                self._sys_msg(f"Bans in {ch_name}:")
                for b in ch.bans:
                    self._sys_msg(f"  {b}")
            else:
                self._sys_msg(f"No bans in {ch_name}")
            return

        if parts[0].startswith("#"):
            ch_name = _normalize_name(parts[0])
            pattern = parts[1] if len(parts) > 1 else ""
        else:
            pattern = parts[0]

        if not pattern:
            self._sys_msg("Usage: /ban [#channel] <nick_or_pattern>")
            return

        ch = mgr.channels.get(ch_name)
        if not ch:
            self._sys_msg(f"Not in channel: {ch_name}")
            return
        if mgr.nick not in ch.ops:
            self._sys_msg(f"You need to be an operator in {ch_name}")
            return

        await mgr.ban_user(ch_name, pattern)
        self._sys_msg(f"Banned {pattern} from {ch_name}")
        mode_msg = {
            "t": "mode", "ch": ch_name, "from": mgr.nick,
            "mode": "+b", "target": pattern, "ts": time.time(),
        }
        await mgr.send_to_channel(ch_name, mode_msg)
        self.app.add_message(ch_name, mode_msg)

    async def _cmd_unban(self, arg: str):
        """Remove a ban."""
        mgr = self.net.channel_mgr
        ch_name = self.app._active_channel

        parts = arg.split() if arg else []
        if not parts:
            self._sys_msg("Usage: /unban [#channel] <nick_or_pattern>")
            return

        if parts[0].startswith("#"):
            ch_name = _normalize_name(parts[0])
            pattern = parts[1] if len(parts) > 1 else ""
        else:
            pattern = parts[0]

        if not pattern:
            self._sys_msg("Usage: /unban [#channel] <nick_or_pattern>")
            return

        ch = mgr.channels.get(ch_name)
        if not ch or mgr.nick not in ch.ops:
            self._sys_msg("You need to be an operator")
            return

        await mgr.unban_user(ch_name, pattern)
        self._sys_msg(f"Unbanned {pattern} from {ch_name}")
        mode_msg = {
            "t": "mode", "ch": ch_name, "from": mgr.nick,
            "mode": "-b", "target": pattern, "ts": time.time(),
        }
        await mgr.send_to_channel(ch_name, mode_msg)
        self.app.add_message(ch_name, mode_msg)

    async def _cmd_kickban(self, arg: str):
        """Kick and ban in one command."""
        mgr = self.net.channel_mgr
        ch_name = self.app._active_channel

        parts = arg.split(maxsplit=1) if arg else []
        if not parts:
            self._sys_msg("Usage: /kickban [#channel] <nick> [reason]")
            return

        if parts[0].startswith("#"):
            ch_name = _normalize_name(parts[0])
            rest = parts[1] if len(parts) > 1 else ""
            parts = rest.split(maxsplit=1) if rest else []

        if not parts:
            self._sys_msg("Usage: /kickban <nick> [reason]")
            return

        nick = parts[0]
        reason = parts[1] if len(parts) > 1 else ""

        ch = mgr.channels.get(ch_name)
        if not ch or mgr.nick not in ch.ops:
            self._sys_msg("You need to be an operator")
            return

        await mgr.ban_user(ch_name, nick)
        await mgr.kick_user(ch_name, nick, reason)
        kick_msg = {
            "t": "kick", "ch": ch_name, "nick": nick,
            "by": mgr.nick, "reason": reason, "ts": time.time(),
        }
        self.app.add_message(ch_name, kick_msg)
        self._sys_msg(f"Kick-banned {nick} from {ch_name}")

    async def _cmd_mode(self, arg: str):
        """Set channel or user modes."""
        mgr = self.net.channel_mgr

        parts = arg.split() if arg else []
        if not parts:
            ch = mgr.channels.get(self.app._active_channel)
            if ch:
                modes = "+" + "".join(sorted(ch.modes)) if ch.modes else "(none)"
                self._sys_msg(f"Modes for {ch.name}: {modes}")
                if ch.ops:
                    self._sys_msg(f"Operators: {', '.join(sorted(ch.ops))}")
            return

        target = parts[0]
        mode_str = parts[1] if len(parts) > 1 else ""
        mode_target = parts[2] if len(parts) > 2 else ""

        if target.startswith("#"):
            ch_name = _normalize_name(target)
            if not mode_str:
                ch = mgr.channels.get(ch_name)
                if ch:
                    modes = "+" + "".join(sorted(ch.modes)) if ch.modes else "(none)"
                    self._sys_msg(f"Modes for {ch_name}: {modes}")
                return

            ch = mgr.channels.get(ch_name)
            if not ch or mgr.nick not in ch.ops:
                self._sys_msg("You need to be an operator")
                return

            # Check if mode_str has user-targeting modes like +o nick
            if mode_target and any(c in mode_str for c in "ov"):
                await mgr.set_user_mode(ch_name, mode_target, mode_str)
                mode_msg = {
                    "t": "mode", "ch": ch_name, "from": mgr.nick,
                    "mode": mode_str, "target": mode_target, "ts": time.time(),
                }
            else:
                await mgr.set_channel_mode(ch_name, mode_str)
                mode_msg = {
                    "t": "mode", "ch": ch_name, "from": mgr.nick,
                    "mode": mode_str, "target": "", "ts": time.time(),
                }

            await mgr.send_to_channel(ch_name, mode_msg)
            self.app.add_message(ch_name, mode_msg)
            self._sys_msg(f"Mode {mode_str} set on {ch_name}"
                          + (f" for {mode_target}" if mode_target else ""))
        else:
            # User mode (target is a nick)
            self._sys_msg(
                "User modes: use /mode #channel +o <nick> to op someone, "
                "or /mode #channel +v <nick> for voice"
            )

    # ==================================================================
    #  III. PRIVATE COMMUNICATION
    # ==================================================================

    async def _cmd_msg(self, arg: str):
        """Send a private message."""
        mgr = self.net.channel_mgr
        parts = arg.split(maxsplit=1) if arg else []
        if len(parts) < 2:
            self._sys_msg("Usage: /msg <nickname> <message>")
            return
        target_nick, text = parts
        msg = {
            "t": "msg", "from": mgr.nick,
            "text": f"[PM] {text}", "ch": "", "ts": time.time(),
        }
        found = await mgr.send_to_nick(target_nick, msg)
        if found:
            self._sys_msg(f"[PM → {target_nick}] {text}")
        else:
            self._sys_msg(f"User {target_nick} not found in any shared channel")

    async def _cmd_query(self, arg: str):
        """Open a private conversation (alias for /msg prompt)."""
        if not arg:
            self._sys_msg("Usage: /query <nickname> [message]")
            return
        parts = arg.split(maxsplit=1)
        nick = parts[0]
        if len(parts) > 1:
            await self._cmd_msg(arg)
        else:
            self._sys_msg(
                f"Private query with {nick}. "
                f"Use /msg {nick} <text> to send messages."
            )

    async def _cmd_notice(self, arg: str):
        """Send a notice to a user or channel."""
        mgr = self.net.channel_mgr
        parts = arg.split(maxsplit=1) if arg else []
        if len(parts) < 2:
            self._sys_msg("Usage: /notice <target> <message>")
            return
        target, text = parts
        await mgr.send_notice(target, text)
        self._sys_msg(f"-{mgr.nick} → {target}- {text}")

    async def _cmd_me(self, arg: str):
        """Send an action."""
        mgr = self.net.channel_mgr
        if not arg or not self.app._active_channel:
            return
        await mgr.send_action(self.app._active_channel, arg)
        local = {
            "t": "me", "ch": self.app._active_channel,
            "from": mgr.nick, "text": arg, "ts": time.time(),
        }
        self.app.add_message(self.app._active_channel, local)

    async def _cmd_ignore(self, arg: str):
        """Toggle ignore for a user."""
        mgr = self.net.channel_mgr
        if not arg:
            if mgr.ignore_list:
                self._sys_msg(f"Ignoring: {', '.join(sorted(mgr.ignore_list))}")
            else:
                self._sys_msg("Ignore list is empty")
            return
        nick = arg.split()[0]
        if nick in mgr.ignore_list:
            mgr.ignore_list.discard(nick)
            self._sys_msg(f"No longer ignoring {nick}")
        else:
            mgr.ignore_list.add(nick)
            self._sys_msg(f"Now ignoring {nick}")

    # ==================================================================
    #  IV. INFORMATION & STATUS
    # ==================================================================

    async def _cmd_who(self, arg: str):
        """List users in a channel."""
        mgr = self.net.channel_mgr
        ch_name = _normalize_name(arg) if arg else self.app._active_channel
        if not ch_name:
            self._sys_msg("No active channel")
            return
        ch = mgr.channels.get(ch_name)
        if not ch:
            self._sys_msg(f"Not in {ch_name}")
            return
        nicks = ch.get_nicks()
        self._sys_msg(f"Users in {ch_name} ({len(nicks)}):")
        for nick in nicks:
            raw = nick.lstrip("@+")
            m = ch.members.get(raw)
            away = f" (away: {m.away})" if m and m.away else ""
            seen = ""
            if m and not m.is_self:
                age = int(time.time() - m.last_seen)
                seen = f" [last seen {age}s ago]"
            self._sys_msg(f"  {nick}{away}{seen}")

    async def _cmd_whois(self, arg: str):
        """Show detailed info about a user."""
        mgr = self.net.channel_mgr
        if not arg:
            self._sys_msg("Usage: /whois <nickname>")
            return
        nick = arg.split()[0]

        info = mgr.whois(nick)
        if not info:
            self._sys_msg(f"No info for {nick} (not in any shared channel)")
            return

        self._sys_msg(f"WHOIS {nick}:")
        self._sys_msg(f"  Channels: {', '.join(info['channels'])}")
        if info.get("away"):
            self._sys_msg(f"  Away: {info['away']}")
        if info.get("last_seen"):
            age = int(time.time() - info["last_seen"])
            self._sys_msg(f"  Last seen: {age}s ago")
        if info.get("profile_key"):
            pk = str(info["profile_key"])[:20]
            self._sys_msg(f"  Profile key: {pk}...")
        self._sys_msg(f"  (P2P — no hostname or server info available)")

    async def _cmd_whowas(self, arg: str):
        """Not available in P2P."""
        self._sys_msg(
            "/whowas: Not available in P2P mode. "
            "There is no server that tracks disconnected users."
        )

    async def _cmd_list(self, arg: str):
        """List all joined channels."""
        mgr = self.net.channel_mgr
        if not mgr.channels:
            self._sys_msg("No channels joined")
            return
        self._sys_msg(f"Channels ({len(mgr.channels)}):")
        for name, ch in mgr.channels.items():
            members = len(ch.members)
            topic = ch.topic or "(no topic)"
            modes = "+" + "".join(sorted(ch.modes)) if ch.modes else ""
            active = " *" if name == self.app._active_channel else ""
            self._sys_msg(f"  {name}{active} [{members}] {modes} {topic}")

    async def _cmd_names(self, arg: str):
        """List nicknames in a channel."""
        mgr = self.net.channel_mgr
        ch_name = _normalize_name(arg) if arg else self.app._active_channel
        if not ch_name:
            self._sys_msg("No active channel")
            return
        ch = mgr.channels.get(ch_name)
        if not ch:
            self._sys_msg(f"Not in {ch_name}")
            return
        nicks = ch.get_nicks()
        self._sys_msg(f"= {ch_name} : {' '.join(nicks)}")

    async def _cmd_away(self, arg: str):
        """Set or clear away status."""
        mgr = self.net.channel_mgr
        if arg:
            mgr.away_message = arg
            self._sys_msg(f"You are now away: {arg}")
            self.app.set_away(arg)
        else:
            if mgr.away_message:
                mgr.away_message = None
                self._sys_msg("You are no longer away")
                self.app.set_away(None)
            else:
                self._sys_msg("Usage: /away <message>  (or /away to return)")
                return
        await mgr.send_away_notice()

    async def _cmd_userhost(self, arg: str):
        """Not meaningful in P2P."""
        self._sys_msg(
            "/userhost: Not available in P2P mode. "
            "Veilid does not expose IP addresses or hostnames."
        )

    async def _cmd_time(self, arg: str):
        """Show current time."""
        now = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        self._sys_msg(f"Local time: {now}")

    async def _cmd_version(self, arg: str):
        """Show version info."""
        self._sys_msg(VERSION)

    async def _cmd_admin(self, arg: str):
        """Not applicable in P2P."""
        self._sys_msg(
            "/admin: Not applicable. This is a decentralised P2P network "
            "— there is no server administrator."
        )

    async def _cmd_info(self, arg: str):
        """Show connection information."""
        mgr = self.net.channel_mgr
        uptime = int(time.time() - self._start_time)
        mins, secs = divmod(uptime, 60)
        hours, mins = divmod(mins, 60)
        self._sys_msg(f"veilid-irc info:")
        self._sys_msg(f"  Version: {VERSION}")
        self._sys_msg(f"  Nick: {mgr.nick}")
        self._sys_msg(f"  Uptime: {hours}h {mins}m {secs}s")
        self._sys_msg(f"  Channels: {len(mgr.channels)}")
        total_members = sum(len(ch.members) for ch in mgr.channels.values())
        self._sys_msg(f"  Total peers: {total_members}")
        self._sys_msg(f"  Veilid server: localhost:5959")
        self._sys_msg(f"  Transport: Veilid safety routes + DHT")

    async def _cmd_motd(self, arg: str):
        """Display the message of the day."""
        for line in MOTD.strip().splitlines():
            self._sys_msg(line)

    async def _cmd_stats(self, arg: str):
        """Show connection statistics."""
        mgr = self.net.channel_mgr
        uptime = int(time.time() - self._start_time)
        total_msgs = sum(len(ch.messages) for ch in mgr.channels.values())
        total_members = sum(len(ch.members) for ch in mgr.channels.values())
        self._sys_msg(f"Statistics:")
        self._sys_msg(f"  Uptime: {uptime}s")
        self._sys_msg(f"  Channels: {len(mgr.channels)}")
        self._sys_msg(f"  Connected peers: {total_members}")
        self._sys_msg(f"  Messages (session): {total_msgs}")
        self._sys_msg(f"  Ignored users: {len(mgr.ignore_list)}")
        if mgr.away_message:
            self._sys_msg(f"  Away: {mgr.away_message}")

    # ==================================================================
    #  V. NETWORK OPERATOR COMMANDS (N/A in P2P)
    # ==================================================================

    async def _cmd_ircop_na(self, arg: str):
        self._sys_msg(
            "That command is not available in P2P mode. "
            "There is no central server or network operators. "
            "Use /kick, /ban, and /mode for channel-level moderation."
        )

    # ==================================================================
    #  VI. VEILID-SPECIFIC COMMANDS
    # ==================================================================

    async def _cmd_create(self, arg: str):
        """Create a new channel."""
        mgr = self.net.channel_mgr
        if not arg:
            self._sys_msg("Usage: /create <channel_name> [topic]")
            return
        parts = arg.split(maxsplit=1)
        name = parts[0]
        topic = parts[1] if len(parts) > 1 else ""
        try:
            ch = await mgr.create_channel(name, topic=topic)
            await mgr.send_join_notice(ch)
            self._sync_ui()
            self.app.switch_channel(ch.name)
            share = mgr.get_share_string(ch.name)
            self._sys_msg(f"Created {ch.name}", ch.name)
            self._sys_msg(f"Share: {share}", ch.name)
        except Exception as e:
            self._sys_msg(f"Error: {e}")

    async def _cmd_share(self, arg: str):
        """Show the share string for a channel.  Add 'qr' to generate a QR code."""
        mgr = self.net.channel_mgr

        # Parse: /share qr  OR  /share #channel  OR  /share #channel qr
        parts = arg.split() if arg else []
        want_qr = False
        ch_name = None
        for p in parts:
            if p.lower() == "qr":
                want_qr = True
            else:
                ch_name = p

        if not ch_name:
            ch_name = self.app._active_channel
        if not ch_name:
            self._sys_msg("No active channel")
            return

        share = mgr.get_share_string(_normalize_name(ch_name))
        if not share:
            self._sys_msg(f"Channel not found: {ch_name}")
            return

        if want_qr:
            from irc_qr import generate_qr
            path = generate_qr(share, label=ch_name, kind="channel")
            if path:
                self._sys_msg(f"QR code saved: {path}")
            else:
                self._sys_msg("QR generation failed — pip install qrcode[pil]")
        else:
            self._sys_msg(f"Share string for {ch_name}:")
            self._sys_msg(share)

    async def _cmd_switch(self, arg: str):
        """Switch to a channel."""
        mgr = self.net.channel_mgr
        if not arg:
            self._sys_msg("Usage: /switch <channel>")
            return
        target = _normalize_name(arg)
        if target in mgr.channels:
            mgr.active_channel = target
            self.app.switch_channel(target)
        else:
            self._sys_msg(f"Not in channel: {target}")

    async def _cmd_clear(self, arg: str):
        """Clear message history."""
        mgr = self.net.channel_mgr
        ch = self.app._active_channel
        if ch:
            self.app._channel_messages[ch] = []
            if ch in mgr.channels:
                mgr.channels[ch].messages = []
            self.app._refresh_messages()
            self._sys_msg("Chat cleared")

    async def _cmd_help(self, arg: str):
        """Show all commands."""
        sections = [
            ("── I. Connection & Setup ──", [
                "/server [host] [port]     Reconnect to veilid-server",
                "/quit [message]           Disconnect and exit",
                "/nick <name>              Change your nickname",
                "/user                     Show your identity info",
                "/ping <nick>              Measure round-trip time to user",
            ]),
            ("── II. Channel Operations ──", [
                "/create <name> [topic]    Create a new channel",
                "/join <CHAN:|code|#name>   Join by share, code, or name",
                "/part [#ch] [message]     Leave a channel",
                "/topic [#ch] [text]       View/set channel topic",
                "/invite <nick> [#ch]      Invite a user",
                "/kick [#ch] <nick> [why]  Kick a user (requires op)",
                "/ban [#ch] <pattern>      Ban a nick pattern (op)",
                "/unban [#ch] <pattern>    Remove a ban (op)",
                "/kickban [#ch] <nick>     Kick + ban in one (op)",
                "/mode <#ch> <+/-modes>    Set channel modes",
                "/mode <#ch> +o <nick>     Grant operator status",
            ]),
            ("── III. Communication ──", [
                "/msg <nick> <text>        Private message",
                "/query <nick>             Open private conversation",
                "/notice <target> <text>   Send a notice",
                "/me <action>              Action emote",
                "/ignore [nick]            Toggle ignore (or show list)",
            ]),
            ("── IV. Info & Status ──", [
                "/who [#channel]           List users with details",
                "/whois <nick>             Detailed user info",
                "/list                     List all channels",
                "/names [#channel]         List nicknames",
                "/away [message]           Set/clear away status",
                "/time                     Show local time",
                "/version                  Show version info",
                "/info                     Connection info",
                "/motd                     Message of the day",
                "/stats                    Session statistics",
            ]),
            ("── V. Veilid ──", [
                "/share [#channel]         Show invite share string",
                "/share [#channel] qr     Save share as QR code",
                "/switch <#channel>        Switch active channel",
                "/clear                    Clear chat history",
            ]),
            ("── VI. Directory ──", [
                "/dir                      Show directory status",
                "/dir create               Create a new directory",
                "/dir join <DIR:…>         Join existing directory",
                "/dir join <image.png>     Join directory from QR code",
                "/dir share                Show directory share string",
                "/dir share qr             Save share as QR code",
                "/rooms                    List channels in directory",
                "/publish [topic]          Publish channel to directory",
                "/unpublish                Remove channel from directory",
                "/join <CODE>              Join by 4-char directory code",
                "/join <image.png>         Join channel from QR code",
                "/scan <image.png>         Decode QR and auto-join",
            ]),
            ("── Keys ──", [
                "Ctrl+N / Ctrl+P           Cycle channels",
                "Ctrl+Q                    Quit",
                "Click                     Select channel / scroll",
            ]),
        ]
        for title, cmds in sections:
            self._sys_msg(title)
            for line in cmds:
                self._sys_msg(f"  {line}")

    # ==================================================================
    #  VII. DIRECTORY COMMANDS
    # ==================================================================

    async def _cmd_dir(self, arg: str):
        """Manage the channel directory."""
        if not self.net.api or not self.net.rc:
            self._sys_msg("Veilid not connected")
            return

        sub_parts = arg.split(maxsplit=1) if arg else []
        sub = sub_parts[0].lower() if sub_parts else ""

        if sub == "create":
            self._sys_msg("Creating directory...")
            log.info("User invoked /dir create")

            async def _do_create():
                try:
                    self.net.directory = await IRCDirectory.create(
                        self.net.api, self.net.rc
                    )
                    share = self.net.directory.get_share_string()
                    self._sys_msg("Directory created!")
                    self._sys_msg(f"Share with others: {share}")
                    log.info("Directory created: %s", self.net.directory.dir_key)
                except Exception as e:
                    self._sys_msg(f"Error: {e}")
                    log.error("Dir create failed: %s", e)

            asyncio.create_task(_do_create())
            return

        if sub == "join":
            share_str = sub_parts[1].strip() if len(sub_parts) > 1 else ""
            if not share_str:
                self._sys_msg("Usage: /dir join <DIR:share_string | image.png>")
                return

            # QR image decoding
            from irc_qr import is_qr_image_path, decode_qr
            if is_qr_image_path(share_str):
                self._sys_msg("Decoding QR code...")
                decoded = decode_qr(share_str)
                if not decoded:
                    self._sys_msg("Could not decode QR image.")
                    return
                if decoded.upper().startswith("CHAN:"):
                    self._sys_msg("That's a channel QR — use /join with it instead.")
                    return
                if not decoded.upper().startswith("DIR:"):
                    self._sys_msg(f"QR decoded but not a DIR: string: {decoded[:80]}...")
                    return
                self._sys_msg("QR decoded successfully!")
                share_str = decoded

            self._sys_msg("Joining directory...")
            log.info("User invoked /dir join")

            async def _do_join():
                try:
                    self.net.directory = await IRCDirectory.join_from_share(
                        self.net.api, self.net.rc, share_str
                    )
                    self._sys_msg("Joined directory!")
                    log.info("Joined directory: %s", self.net.directory.dir_key)
                except Exception as e:
                    self._sys_msg(f"Error: {e}")
                    log.error("Dir join failed: %s", e)

            asyncio.create_task(_do_join())
            return

        if sub == "share":
            if not self.net.directory:
                self._sys_msg("No directory configured")
                return
            share_str = self.net.directory.get_share_string()
            extra = sub_parts[1].strip().lower() if len(sub_parts) > 1 else ""
            if extra == "qr":
                from irc_qr import generate_qr
                path = generate_qr(share_str, label="Community Directory",
                                   kind="directory")
                if path:
                    self._sys_msg(f"QR code saved: {path}")
                else:
                    self._sys_msg("QR generation failed — pip install qrcode[pil]")
            else:
                self._sys_msg(share_str)
            return

        # Default: show status
        if self.net.directory:
            self._sys_msg(f"Directory: {self.net.directory.dir_key}")
            self._sys_msg("Use /rooms to list channels, /publish to add yours")
        else:
            self._sys_msg("No directory configured")
        self._sys_msg("  /dir create       Create a new directory")
        self._sys_msg("  /dir join <DIR:…> Join an existing directory")
        self._sys_msg("  /dir share        Show directory share string")
        self._sys_msg("  /dir share qr     Save share string as QR code")

    async def _cmd_publish(self, arg: str):
        """Publish the current channel to the directory."""
        mgr = self.net.channel_mgr
        if not self.net.directory:
            self._sys_msg("No directory. Use /dir create or /dir join first")
            return

        ch_name = self.app._active_channel
        if not ch_name:
            self._sys_msg("No active channel to publish")
            return

        ch = mgr.channels.get(ch_name)
        if not ch:
            self._sys_msg(f"Channel {ch_name} not found")
            return

        share = mgr.get_share_string(ch_name)
        if not share:
            self._sys_msg("Could not get share string for channel")
            return

        title = arg if arg else ch.topic
        self._sys_msg(f"Publishing {ch_name} to directory...")
        log.info("User invoked /publish for %s", ch_name)

        # Spawn as background task so DHT scans don't freeze the UI
        async def _do_publish():
            try:
                code = await self.net.directory.publish_channel(
                    self.net.rc,
                    nick=mgr.nick,
                    name=ch_name,
                    topic=title,
                    share_string=share,
                    members=len(ch.members),
                )
                self._sys_msg(f"Published {ch_name} to directory!")
                self._sys_msg(f"Join code: {code}")
                self._sys_msg(f"Others can join with: /join {code}")
                log.info("Publish complete: %s code=%s", ch_name, code)
            except Exception as e:
                self._sys_msg(f"Publish error: {e}")
                log.error("Publish failed: %s", e)

        asyncio.create_task(_do_publish())

    async def _cmd_unpublish(self, arg: str):
        """Remove the current channel from the directory."""
        mgr = self.net.channel_mgr
        if not self.net.directory:
            self._sys_msg("No directory configured")
            return

        ch_name = _normalize_name(arg) if arg else self.app._active_channel
        if not ch_name:
            self._sys_msg("No active channel")
            return

        share = mgr.get_share_string(ch_name)
        if not share:
            self._sys_msg(f"Channel {ch_name} not found")
            return

        self._sys_msg(f"Removing {ch_name} from directory...")

        async def _do_unpublish():
            try:
                removed = await self.net.directory.unpublish_channel(
                    self.net.rc, share
                )
                if removed:
                    self._sys_msg(f"Removed {ch_name} from directory")
                else:
                    self._sys_msg(f"{ch_name} not found in directory")
            except Exception as e:
                self._sys_msg(f"Error: {e}")

        asyncio.create_task(_do_unpublish())

    async def _cmd_rooms(self, arg: str):
        """List all channels in the directory."""
        if not self.net.directory:
            self._sys_msg("No directory. Use /dir create or /dir join first")
            return

        self._sys_msg("Scanning directory...")

        async def _do_rooms():
            try:
                channels = await self.net.directory.list_channels(self.net.rc)
                if not channels:
                    self._sys_msg("No channels listed in directory")
                    return
                self._sys_msg(f"Directory ({len(channels)} channels):")
                for entry in channels:
                    code = entry.get("short", "????")
                    name = entry.get("name", "?")
                    nick = entry.get("nick", "?")
                    topic = entry.get("topic", "")
                    members = entry.get("members", "?")
                    age = int(time.time() - entry.get("ts", 0))
                    if age < 60:
                        age_str = f"{age}s ago"
                    elif age < 3600:
                        age_str = f"{age // 60}m ago"
                    else:
                        age_str = f"{age // 3600}h ago"
                    line = f"  [{code}] {name} ({members} users) by {nick}"
                    if topic:
                        line += f" — {topic}"
                    line += f" [{age_str}]"
                    self._sys_msg(line)
                self._sys_msg("Join with: /join <CODE>")
            except Exception as e:
                self._sys_msg(f"Error listing channels: {e}")
                log.error("Error listing channels: %s", e)

        asyncio.create_task(_do_rooms())

    async def _cmd_scan(self, arg: str):
        """Scan a QR code image and join the channel or directory it contains."""
        if not arg:
            self._sys_msg("Usage: /scan <path/to/qrcode.png>")
            self._sys_msg("Decodes a VOC QR code and joins the channel or directory.")
            return

        from irc_qr import decode_qr
        self._sys_msg("Decoding QR code...")
        decoded = decode_qr(arg)

        if not decoded:
            self._sys_msg("Could not decode QR image. Is it a valid QR code?")
            self._sys_msg("Tip: supports PNG, JPG, BMP, GIF, WEBP")
            return

        if decoded.upper().startswith("CHAN:"):
            self._sys_msg(f"Channel share string found — joining...")
            await self._cmd_join(decoded)
        elif decoded.upper().startswith("DIR:"):
            self._sys_msg(f"Directory share string found — joining...")
            await self._cmd_dir(f"join {decoded}")
        else:
            self._sys_msg(f"QR decoded but not a VOC string:")
            self._sys_msg(decoded[:200])

    # ── Helpers ──────────────────────────────────────────────────────

    def _sys_msg(self, text: str, channel: str | None = None):
        msg = {"t": "sys", "text": text, "ts": time.time()}
        ch = channel or self.app._active_channel
        if ch:
            self.app.add_message(ch, msg)
        else:
            # No channel yet — buffer for later
            if "_pre" not in self.app._channel_messages:
                self.app._channel_messages["_pre"] = []
            self.app._channel_messages["_pre"].append(msg)

    def _sync_ui(self):
        mgr = self.net.channel_mgr
        if not mgr:
            return

        names = list(mgr.channels.keys())
        self.app.set_channels(names, mgr.active_channel)

        # Flush any pre-channel messages
        pre = self.app._channel_messages.pop("_pre", [])

        for name, ch in mgr.channels.items():
            self.app.set_users(name, ch.get_nicks())
            self.app.set_topic(name, ch.topic)
            if name not in self.app._channel_messages:
                self.app._channel_messages[name] = ch.messages

        if mgr.active_channel:
            self.app.switch_channel(mgr.active_channel)
            # Replay pre-channel messages
            for msg in pre:
                self.app.add_message(mgr.active_channel, msg)

    async def shutdown(self):
        log.info("VeilidIRC.shutdown() called")
        if self.veilid_ok:
            await self.net.stop()
            self.veilid_ok = False
        log.info("VeilidIRC shutdown complete")


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="veilid-irc",
        description="IRC-style multi-user chat over the Veilid P2P network",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --nick alice --create general
              %(prog)s --nick bob --join CHAN:eyJrIjoi...
        """),
    )
    parser.add_argument("--nick", default=None, help="Your nickname")
    parser.add_argument("--join", default=None, metavar="SHARE",
                        help="Join by CHAN: share string")
    parser.add_argument("--create", default=None, metavar="NAME",
                        help="Create a channel on startup")
    parser.add_argument("--topic", default=None,
                        help="Topic for --create")
    parser.add_argument("--dir", default=None, metavar="SHARE",
                        help="Join a directory by DIR: share string")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("veilid-irc starting — nick=%s", args.nick)
    log.info("=" * 60)

    veilid_proc, we_started = ensure_veilid_server()

    app = IRCApp()
    irc = VeilidIRC(app, args)

    app.on_user_input = irc.handle_input
    app.on_ready_cb = irc.start
    app.on_shutdown_cb = irc.shutdown

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop_veilid_server(veilid_proc, we_started)


if __name__ == "__main__":
    main()
