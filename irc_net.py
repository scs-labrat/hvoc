"""Veilid P2P networking layer for IRC-style multi-user chat.

Handles:
  - Connection to local veilid-server daemon (localhost:5959)
  - Waiting for network readiness (PublicInternet route allocation)
  - Private route creation for receiving messages
  - Incoming app_message decompression and dispatch to ChannelManager

Routing context lifecycle:
  Safety routing is ENABLED BY DEFAULT in modern Veilid (issue #339).
  We only call new_routing_context() — no with_default_safety() needed.
  This avoids the _JsonRoutingContext.__del__ assertion on Windows where
  GC can run after the event loop is closed.
"""

import asyncio
import json
import zlib

import veilid

from irc_channel import ChannelManager
from irc_log import get_logger

log = get_logger(__name__)

# How long to wait for veilid-server to establish network connectivity
NETWORK_READY_TIMEOUT = 120   # seconds
NETWORK_POLL_INTERVAL = 2     # seconds between state checks

# Attachment states that mean we can allocate routes.
# Any state containing "Attached" or "Fully" means the node is on the network.
# Only "Detached", "Detaching", and "Attaching" are not ready.
_NOT_READY_STATES = {"Detached", "Detaching", "Attaching"}


def _is_network_ready(state: str) -> bool:
    """Return True if the attachment state indicates route allocation is possible."""
    return state not in _NOT_READY_STATES and "tach" in state.lower()


class IRCNet:
    """Manages the Veilid connection and bridges to ChannelManager."""

    def __init__(self):
        self.api = None
        self.rc = None             # single routing context (safety is default)
        self.my_route = None
        self.channel_mgr: ChannelManager | None = None
        self.directory = None      # IRCDirectory (optional)
        self.running = False

        # Network state tracking
        self._attachment_state = "Detached"
        self._network_ready = asyncio.Event()

        # External callbacks
        self.on_status = None       # (text) -> None
        self.on_message = None      # (channel, msg_dict) -> None

        self._msg_queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Veilid update callback
    # ------------------------------------------------------------------
    async def _update_callback(self, update: veilid.VeilidUpdate):
        if update.kind == veilid.VeilidUpdateKind.APP_MESSAGE:
            await self._msg_queue.put(update.detail.message)

        elif update.kind == veilid.VeilidUpdateKind.ATTACHMENT:
            state = str(update.detail.state) if hasattr(update.detail, 'state') else str(update.detail)
            # Extract just the state name from enum repr
            if "." in state:
                state = state.rsplit(".", 1)[-1]
            old = self._attachment_state
            self._attachment_state = state
            log.info("Attachment state: %s → %s", old, state)
            self._notify(f"Network: {state}")
            if _is_network_ready(state):
                self._network_ready.set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, nick: str, profile_key=None):
        """Connect to veilid-server and initialize ChannelManager."""
        self.running = True
        self._notify("Connecting to veilid-server...")
        log.info("Connecting to veilid-server localhost:5959...")

        self.api = await veilid.json_api_connect(
            "localhost", 5959, self._update_callback
        )
        log.info("JSON API connected")

        # Safety routing is the DEFAULT in modern Veilid.
        self.rc = await self.api.new_routing_context()
        log.info("Routing context created (safety is default)")

        # Wait for veilid-server to establish network connectivity.
        # new_private_route() will fail with "Try again" until the node
        # has a valid PublicInternet network class.
        await self._wait_for_network()

        # Create our private route for receiving messages (with retries)
        self.my_route = await self._create_route_with_retry()
        log.info("Private route created")

        # Initialize channel manager
        self.channel_mgr = ChannelManager(
            api=self.api,
            rc=self.rc,
            my_route=self.my_route,
            nick=nick,
            profile_key=profile_key,
        )
        self.channel_mgr.on_status = self.on_status
        self.channel_mgr.on_message = self.on_message

        # Start receive loop
        self._tasks.append(asyncio.create_task(self._receive_loop()))

        self._notify("Connected to Veilid network")

    async def _wait_for_network(self):
        """Wait until veilid-server has established PublicInternet connectivity."""
        # Check current state first
        try:
            state = await self.api.get_state()
            attachment = state.attachment
            state_name = str(attachment.state) if hasattr(attachment, 'state') else str(attachment)
            if "." in state_name:
                state_name = state_name.rsplit(".", 1)[-1]
            self._attachment_state = state_name
            log.info("Current attachment state: %s", state_name)

            if _is_network_ready(state_name):
                log.info("Network already ready")
                self._network_ready.set()
                return
        except Exception as e:
            log.warning("Could not get initial state: %s", e)

        # Wait for attachment update
        self._notify("Waiting for Veilid network (this may take a minute)...")
        log.info("Waiting up to %ds for network readiness...", NETWORK_READY_TIMEOUT)

        try:
            await asyncio.wait_for(
                self._network_ready.wait(),
                timeout=NETWORK_READY_TIMEOUT,
            )
            log.info("Network ready! State: %s", self._attachment_state)
            self._notify(f"Network ready ({self._attachment_state})")
        except asyncio.TimeoutError:
            log.warning("Network readiness timeout after %ds (state: %s)",
                        NETWORK_READY_TIMEOUT, self._attachment_state)
            self._notify(f"Network timeout — trying anyway ({self._attachment_state})")
            # Don't raise — try anyway, the route creation has its own retries

    async def _create_route_with_retry(self, max_attempts: int = 10,
                                        delay: float = 3.0):
        """Create a private route with retries for transient network errors."""
        for attempt in range(1, max_attempts + 1):
            try:
                route = await self.api.new_private_route()
                log.info("Private route allocated on attempt %d", attempt)
                return route
            except Exception as e:
                err_str = str(e).lower()
                if "try again" in err_str or "unable to allocate" in err_str:
                    if attempt < max_attempts:
                        log.info("Route allocation attempt %d/%d failed: %s "
                                 "(retrying in %.0fs)",
                                 attempt, max_attempts, e, delay)
                        self._notify(
                            f"Waiting for network... ({attempt}/{max_attempts})"
                        )
                        await asyncio.sleep(delay)
                        continue
                log.error("Route allocation failed after %d attempts: %s",
                          attempt, e)
                raise

    async def stop(self):
        """Graceful shutdown — release ALL Veilid resources in order.

        Must be called while the event loop is still running.
        """
        log.info("IRCNet.stop() — beginning shutdown...")
        self.running = False

        # 1. Channel manager (sends quit notices, clears DHT slots)
        if self.channel_mgr:
            try:
                log.debug("Shutting down channel manager...")
                await self.channel_mgr.shutdown()
                log.debug("Channel manager shutdown complete")
            except Exception as e:
                log.warning("Channel manager shutdown error: %s", e)
            self.channel_mgr = None

        # 2. Cancel background tasks and wait for them
        log.debug("Cancelling %d background tasks...", len(self._tasks))
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        # 3. Release private route
        if self.my_route and self.api:
            try:
                log.debug("Releasing private route...")
                await self.api.release_private_route(self.my_route.route_id)
            except Exception as e:
                log.debug("Private route release: %s", e)
            self.my_route = None

        # 4. Release routing context
        if self.rc is not None:
            try:
                log.debug("Releasing routing context...")
                await self.rc.release()
            except Exception as e:
                log.debug("Routing context release: %s", e)
            self.rc = None

        # 5. Release API connection last
        if self.api is not None:
            try:
                log.debug("Releasing API connection...")
                await self.api.release()
            except Exception as e:
                log.debug("API release: %s", e)
            self.api = None

        log.info("IRCNet shutdown complete")

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------
    async def _receive_loop(self):
        """Drain incoming app_messages, decompress, and dispatch."""
        while self.running:
            try:
                raw = await asyncio.wait_for(self._msg_queue.get(), timeout=0.1)
                try:
                    msg = json.loads(zlib.decompress(raw).decode())
                except Exception:
                    try:
                        msg = json.loads(raw.decode())
                    except Exception:
                        continue

                if self.channel_mgr:
                    self.channel_mgr.dispatch_message(msg)

            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _notify(self, text: str):
        if self.on_status:
            self.on_status(text)
