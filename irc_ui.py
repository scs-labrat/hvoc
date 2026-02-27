"""Textual-based IRC terminal UI.

Cross-platform (Windows, Linux, macOS) with no curses dependency.
Uses Rich markup for colored nicks, timestamps, and system messages.

Layout
------
┌──────────┬──────────────────────────────┬──────────┐
│ CHANNELS │  #general — Welcome!         │  USERS   │
│          ├──────────────────────────────┤          │
│ #general │ [12:34] <alice> hello        │ @alice   │
│ #random  │ [12:35] <bob> hey!           │ bob      │
│          │ [12:35] * alice waves        │ charlie  │
│          │                              │          │
├──────────┴──────────────────────────────┴──────────┤
│ [#general] > _                                     │
└────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import time

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

# ── Nick color palette (Rich color names) ────────────────────────────
NICK_COLORS = [
    "red", "green", "yellow", "blue", "magenta", "cyan",
    "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan",
    "orange1", "deep_pink1", "spring_green1", "dodger_blue1",
]


def _nick_color(nick: str) -> str:
    """Deterministic Rich color for a nick."""
    h = sum(ord(c) for c in nick) % len(NICK_COLORS)
    return NICK_COLORS[h]


def _escape(text: str) -> str:
    """Escape Rich markup characters in user text."""
    return text.replace("[", r"\[").replace("]", r"\]")


def format_msg(msg: dict) -> str:
    """Format a message dict into a Rich-markup string for the RichLog."""
    kind = msg.get("t", "msg")
    ts = msg.get("ts")
    ts_str = time.strftime("%H:%M", time.localtime(ts)) if ts else "     "
    dim_ts = f"[dim]{ts_str}[/dim]"

    if kind == "msg":
        sender = msg.get("from", "???")
        color = _nick_color(sender)
        text = _escape(msg.get("text", ""))
        return f"{dim_ts} [bold {color}]<{sender}>[/bold {color}] {text}"

    if kind == "me":
        sender = msg.get("from", "???")
        color = _nick_color(sender)
        text = _escape(msg.get("text", ""))
        return f"{dim_ts} [italic {color}]* {sender} {text}[/italic {color}]"

    if kind == "notice":
        sender = msg.get("from", "???")
        color = _nick_color(sender)
        text = _escape(msg.get("text", ""))
        return (
            f"{dim_ts} [bold yellow]-{sender}-[/bold yellow] {text}"
        )

    if kind == "join":
        nick = msg.get("nick", "???")
        color = _nick_color(nick)
        return (
            f"{dim_ts} [green]-->[/green] "
            f"[{color}]{nick}[/{color}] [green]has joined[/green]"
        )

    if kind == "part":
        nick = msg.get("nick", "???")
        color = _nick_color(nick)
        reason = msg.get("reason", "")
        suffix = f" ({_escape(reason)})" if reason else ""
        return (
            f"{dim_ts} [red]<--[/red] "
            f"[{color}]{nick}[/{color}] [red]has left{suffix}[/red]"
        )

    if kind == "quit":
        nick = msg.get("nick", "???")
        color = _nick_color(nick)
        reason = msg.get("reason", "")
        suffix = f" ({_escape(reason)})" if reason else ""
        return (
            f"{dim_ts} [red]<--[/red] "
            f"[{color}]{nick}[/{color}] [red]has quit{suffix}[/red]"
        )

    if kind == "kick":
        nick = msg.get("nick", "???")
        by = msg.get("by", "???")
        reason = msg.get("reason", "")
        suffix = f" ({_escape(reason)})" if reason else ""
        return (
            f"{dim_ts} [bold red]<<![/bold red] "
            f"{_escape(nick)} was kicked by {_escape(by)}{suffix}"
        )

    if kind == "nick":
        old = msg.get("old", "?")
        new = msg.get("new", "?")
        return (
            f"{dim_ts} [cyan]---[/cyan] "
            f"{_escape(old)} is now known as [bold]{_escape(new)}[/bold]"
        )

    if kind == "topic":
        sender = msg.get("from", "???")
        text = _escape(msg.get("text", ""))
        return (
            f"{dim_ts} [cyan]---[/cyan] "
            f"{_escape(sender)} changed topic to: [italic]{text}[/italic]"
        )

    if kind == "mode":
        by = msg.get("from", "???")
        mode = msg.get("mode", "")
        target = msg.get("target", "")
        return (
            f"{dim_ts} [cyan]---[/cyan] "
            f"{_escape(by)} sets mode [bold]{_escape(mode)}[/bold]"
            f"{' on ' + _escape(target) if target else ''}"
        )

    if kind == "away":
        nick = msg.get("nick", "???")
        away_msg = msg.get("message")
        if away_msg:
            return (
                f"{dim_ts} [dim]---[/dim] "
                f"{_escape(nick)} is away: {_escape(away_msg)}"
            )
        return (
            f"{dim_ts} [dim]---[/dim] "
            f"{_escape(nick)} is back"
        )

    if kind == "invite":
        from_nick = msg.get("from", "???")
        ch = msg.get("ch", "???")
        return (
            f"{dim_ts} [bold magenta]>>>[/bold magenta] "
            f"{_escape(from_nick)} invited you to {_escape(ch)}"
        )

    if kind == "sys":
        text = _escape(msg.get("text", ""))
        return f"{dim_ts} [dim cyan]---[/dim cyan] [dim]{text}[/dim]"

    return f"{dim_ts} {_escape(str(msg))}"


# =====================================================================
# Widgets
# =====================================================================

class ChannelItem(ListItem):
    """A clickable channel entry in the sidebar."""

    def __init__(self, name: str, unread: int = 0, active: bool = False):
        super().__init__()
        self.channel_name = name
        self.unread = unread
        self.is_active = active

    def compose(self) -> ComposeResult:
        label = self.channel_name
        if self.unread > 0:
            label += f" ({self.unread})"
        yield Label(label, id="chan-label")


class TopicBar(Static):
    """Displays the current channel name and topic."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._channel = ""
        self._topic = ""

    def set_content(self, channel: str, topic: str = ""):
        self._channel = channel
        self._topic = topic
        if topic:
            self.update(f" {_escape(channel)} — {_escape(topic)}")
        else:
            self.update(f" {_escape(channel)}")


class UserList(Static):
    """Displays the list of users in the current channel."""

    def set_users(self, users: list[str]):
        if not users:
            self.update("[dim]No users[/dim]")
            return
        lines = []
        for nick in users:
            # Strip @ prefix for color lookup
            raw_nick = nick.lstrip("@+")
            prefix = nick[:len(nick) - len(raw_nick)]
            color = _nick_color(raw_nick)
            if prefix:
                lines.append(
                    f"[bold]{_escape(prefix)}[/bold]"
                    f"[{color}]{_escape(raw_nick)}[/{color}]"
                )
            else:
                lines.append(f"[{color}]{_escape(raw_nick)}[/{color}]")
        self.update("\n".join(lines))


class StatusBar(Static):
    """Bottom status bar showing nick, away status, and connection info."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._nick = "anon"
        self._status = "Connecting..."
        self._count = 0
        self._away = None

    def set_info(self, nick: str | None = None, status: str | None = None,
                 channel_count: int | None = None, away: str | None = ...):
        if nick is not None:
            self._nick = nick
        if status is not None:
            self._status = status
        if channel_count is not None:
            self._count = channel_count
        if away is not ...:
            self._away = away

        away_str = f" [italic](away: {_escape(self._away)})[/italic]" if self._away else ""
        self.update(
            f" [bold]{_escape(self._nick)}[/bold]{away_str} │ "
            f"{_escape(self._status)}  ─  {self._count} channel(s)"
        )


# =====================================================================
# Main App
# =====================================================================

class IRCApp(App):
    """Textual IRC client application."""

    TITLE = "veilid-irc"

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-area {
        height: 1fr;
    }

    #sidebar {
        width: 18;
        border-right: solid $surface-lighten-2;
        background: $surface-darken-1;
    }

    #sidebar-header {
        height: 1;
        background: $accent;
        color: $text;
        text-style: bold;
        padding: 0 1;
    }

    #channel-list {
        height: 1fr;
        scrollbar-size: 1 1;
    }

    #channel-list > ListItem {
        padding: 0 1;
        height: 1;
    }

    #channel-list > ListItem.active-channel {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    #channel-list > ListItem.unread-channel {
        text-style: bold;
        color: $warning;
    }

    #center {
        width: 1fr;
    }

    #topic-bar {
        height: 1;
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
        padding: 0 0;
    }

    #message-log {
        height: 1fr;
        border: none;
        padding: 0 1;
        scrollbar-size: 1 1;
    }

    #user-panel {
        width: 16;
        border-left: solid $surface-lighten-2;
        background: $surface-darken-1;
    }

    #user-header {
        height: 1;
        background: $accent;
        color: $text;
        text-style: bold;
        padding: 0 1;
    }

    #user-list {
        height: 1fr;
        padding: 0 1;
    }

    #status-bar {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 0;
    }

    #input-box {
        height: 1;
        border: none;
    }

    #input-box:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "next_channel", "Next Ch", show=True),
        Binding("ctrl+p", "prev_channel", "Prev Ch", show=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._channels: list[str] = []
        self._channel_messages: dict[str, list[dict]] = {}
        self._channel_users: dict[str, list[str]] = {}
        self._channel_topics: dict[str, str] = {}
        self._channel_unread: dict[str, int] = {}
        self._active_channel: str = ""
        self._my_nick: str = "anon"

        # External callbacks
        self.on_user_input = None   # async callable(text: str)
        self.on_ready_cb = None     # async callable() — called after mount
        self.on_shutdown_cb = None  # async callable() — called before exit

    # ── Compose ──
    def compose(self) -> ComposeResult:
        with Horizontal(id="main-area"):
            with Vertical(id="sidebar"):
                yield Static(" CHANNELS", id="sidebar-header")
                yield ListView(id="channel-list")
            with Vertical(id="center"):
                yield TopicBar(id="topic-bar")
                yield RichLog(
                    id="message-log",
                    highlight=False,
                    markup=True,
                    wrap=True,
                    max_lines=2000,
                    auto_scroll=True,
                )
            with Vertical(id="user-panel"):
                yield Static(" USERS", id="user-header")
                yield UserList(id="user-list")

        yield StatusBar(id="status-bar")
        yield Input(placeholder="Type a message or /help ...", id="input-box")

    def on_mount(self) -> None:
        self.query_one("#input-box", Input).focus()
        if self.on_ready_cb:
            asyncio.create_task(self.on_ready_cb())

    # ── Public API ──

    def set_nick(self, nick: str):
        self._my_nick = nick
        self._update_status_bar()

    def set_status(self, text: str):
        try:
            sb = self.query_one("#status-bar", StatusBar)
            sb.set_info(status=text)
        except Exception:
            pass

    def set_away(self, message: str | None):
        try:
            sb = self.query_one("#status-bar", StatusBar)
            sb.set_info(away=message)
        except Exception:
            pass

    def set_channels(self, names: list[str], active: str | None = None):
        self._channels = names
        if active is not None:
            self._active_channel = active
        self._refresh_channel_list()
        self._update_status_bar()

    def switch_channel(self, name: str):
        if name not in self._channels:
            return
        self._active_channel = name
        self._channel_unread[name] = 0
        self._refresh_channel_list()
        self._refresh_messages()
        self._refresh_users()
        self._refresh_topic()
        try:
            self.query_one("#input-box", Input).placeholder = f"Message {name}..."
        except Exception:
            pass

    def add_message(self, channel: str, msg: dict):
        if channel not in self._channel_messages:
            self._channel_messages[channel] = []
        self._channel_messages[channel].append(msg)

        if len(self._channel_messages[channel]) > 2000:
            self._channel_messages[channel] = self._channel_messages[channel][-1500:]

        if channel == self._active_channel:
            try:
                log = self.query_one("#message-log", RichLog)
                log.write(format_msg(msg))
            except Exception:
                pass
        else:
            self._channel_unread[channel] = (
                self._channel_unread.get(channel, 0) + 1
            )
            self._refresh_channel_list()

    def set_users(self, channel: str, nicks: list[str]):
        self._channel_users[channel] = nicks
        if channel == self._active_channel:
            self._refresh_users()

    def set_topic(self, channel: str, topic: str):
        self._channel_topics[channel] = topic
        if channel == self._active_channel:
            self._refresh_topic()

    # ── Internal refresh ──

    def _refresh_channel_list(self):
        try:
            lv = self.query_one("#channel-list", ListView)
            lv.clear()
            for name in self._channels:
                unread = self._channel_unread.get(name, 0)
                active = (name == self._active_channel)
                item = ChannelItem(name, unread=unread, active=active)
                if active:
                    item.add_class("active-channel")
                elif unread > 0:
                    item.add_class("unread-channel")
                lv.append(item)
        except Exception:
            pass

    def _refresh_messages(self):
        try:
            log = self.query_one("#message-log", RichLog)
            log.clear()
            msgs = self._channel_messages.get(self._active_channel, [])
            for msg in msgs:
                log.write(format_msg(msg))
        except Exception:
            pass

    def _refresh_users(self):
        try:
            ul = self.query_one("#user-list", UserList)
            ul.set_users(self._channel_users.get(self._active_channel, []))
        except Exception:
            pass

    def _refresh_topic(self):
        try:
            tb = self.query_one("#topic-bar", TopicBar)
            tb.set_content(
                self._active_channel,
                self._channel_topics.get(self._active_channel, ""),
            )
        except Exception:
            pass

    def _update_status_bar(self):
        try:
            sb = self.query_one("#status-bar", StatusBar)
            sb.set_info(nick=self._my_nick, channel_count=len(self._channels))
        except Exception:
            pass

    # ── Event handlers ──

    @on(ListView.Selected, "#channel-list")
    def on_channel_selected(self, event: ListView.Selected):
        item = event.item
        if isinstance(item, ChannelItem):
            self.switch_channel(item.channel_name)

    @on(Input.Submitted, "#input-box")
    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        if self.on_user_input:
            await self.on_user_input(text)

    # ── Key bindings ──

    def action_next_channel(self):
        if self._channels and self._active_channel:
            try:
                idx = self._channels.index(self._active_channel)
            except ValueError:
                return
            next_idx = (idx + 1) % len(self._channels)
            self.switch_channel(self._channels[next_idx])

    def action_prev_channel(self):
        if self._channels and self._active_channel:
            try:
                idx = self._channels.index(self._active_channel)
            except ValueError:
                return
            prev_idx = (idx - 1) % len(self._channels)
            self.switch_channel(self._channels[prev_idx])

    def action_quit_app(self):
        # Schedule cleanup then exit
        asyncio.create_task(self._do_quit())

    async def _do_quit(self):
        """Run cleanup callback then exit."""
        if self.on_shutdown_cb:
            try:
                await self.on_shutdown_cb()
            except Exception:
                pass
        self.exit()
