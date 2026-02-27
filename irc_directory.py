"""IRC channel directory using a shared-owner-keypair DHT record.

Adapted from the ASCII webcam chat's directory.py pattern.

Directory DHT record (veilid.DHTSchema.dflt(64)):
  subkey 0   : {"name": "public", "created": ts, "v": 2}
  subkeys 1-63: channel entries or empty

Each channel entry:
  {
    "nick": "alice",            # who published it
    "name": "#general",         # channel name
    "topic": "Welcome!",        # channel topic
    "share": "CHAN:eyJrIjoi…",  # full share string (includes keypair)
    "short": "A7K2",            # deterministic 4-char join code
    "members": 3,               # approximate member count at publish time
    "ts": 1234567890.0          # last updated
  }

Shared-owner model: the directory creator shares both the DHT key AND
the owner keypair.  Anyone with the keypair can write to any subkey.
The keypair acts as a community write token.

Share string format: DIR:<base64(json({"k": dir_key, "p": dir_keypair}))>

Local persistence via TableDb("veilid_irc_directory", 1):
  Keys: dir_key, dir_keypair
"""

import asyncio
import base64
import hashlib
import json
import time

import veilid

from irc_log import get_logger

log = get_logger(__name__)


# Characters for 4-char short codes (unambiguous uppercase, no O/0/I/1)
_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# How old an entry can be before it's considered stale (24 hours)
STALE_AGE = 86400

# Per-DHT-call timeout (seconds) — prevents a single slow read from hanging
DHT_TIMEOUT = 10.0


def _generate_short_code(key_str: str) -> str:
    """Derive a deterministic 4-char short code from a string."""
    h = hashlib.sha256(key_str.encode()).digest()
    code = []
    for i in range(4):
        code.append(_CODE_CHARS[h[i] % len(_CODE_CHARS)])
    return "".join(code)


async def _timed_get(rc, key, subkey: int, force: bool = False,
                     timeout: float = DHT_TIMEOUT):
    """get_dht_value with a timeout.  Returns None on timeout/error."""
    try:
        return await asyncio.wait_for(
            rc.get_dht_value(key, veilid.ValueSubkey(subkey), force),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning("DHT read timeout: subkey=%d (%.1fs)", subkey, timeout)
        return None
    except Exception as e:
        log.debug("DHT read error: subkey=%d: %s", subkey, e)
        return None


async def _timed_set(rc, key, subkey: int, data: bytes, writer_keypair,
                     timeout: float = DHT_TIMEOUT):
    """set_dht_value with a timeout."""
    try:
        opts = veilid.SetDHTValueOptions(writer=writer_keypair)
        await asyncio.wait_for(
            rc.set_dht_value(key, veilid.ValueSubkey(subkey), data, options=opts),
            timeout=timeout,
        )
        return True
    except asyncio.TimeoutError:
        log.warning("DHT write timeout: subkey=%d (%.1fs)", subkey, timeout)
        return False
    except Exception as e:
        log.warning("DHT write error: subkey=%d: %s", subkey, e)
        return False


class IRCDirectory:
    """Manages a community channel directory on the Veilid DHT."""

    MAX_SLOTS = 63   # subkeys 1-63

    def __init__(self):
        self.dir_key = None          # TypedKey of the directory DHT record
        self.dir_keypair = None      # KeyPair for write access
        self._db = None              # TableDb handle

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def load(cls, api, rc):
        """Load saved directory from TableDb.  Returns IRCDirectory or None."""
        log.debug("Loading directory from TableDb...")
        self = cls()
        self._db = await api.open_table_db("veilid_irc_directory", 1)

        raw_key = await self._db.load(b"dir_key")
        raw_kp = await self._db.load(b"dir_keypair")

        if raw_key and raw_kp:
            self.dir_key = veilid.RecordKey(raw_key.decode())
            self.dir_keypair = veilid.KeyPair(raw_kp.decode())
            try:
                log.debug("Opening existing directory DHT record: %s", self.dir_key)
                await rc.open_dht_record(self.dir_key, writer=self.dir_keypair)
                log.info("Directory loaded: %s", self.dir_key)
            except Exception as e:
                log.warning("Failed to open directory record: %s", e)
                return None
            return self
        log.debug("No saved directory found")
        return None

    @classmethod
    async def create(cls, api, rc):
        """Create a new directory DHT record."""
        log.info("Creating new directory DHT record...")
        self = cls()
        self._db = await api.open_table_db("veilid_irc_directory", 1)

        schema = veilid.DHTSchema.dflt(64)
        record = await rc.create_dht_record(
            veilid.CryptoKind.CRYPTO_KIND_VLD0, schema
        )
        self.dir_key = record.key
        self.dir_keypair = record.owner_key_pair()
        log.info("Directory created: key=%s", self.dir_key)

        # Write directory header
        header = {"name": "public", "type": "irc", "created": time.time(), "v": 2}
        opts = veilid.SetDHTValueOptions(writer=self.dir_keypair)
        await rc.set_dht_value(
            self.dir_key, veilid.ValueSubkey(0),
            json.dumps(header).encode(), options=opts
        )

        # Persist locally
        await self._db.store(b"dir_key", str(self.dir_key).encode())
        await self._db.store(b"dir_keypair", str(self.dir_keypair).encode())

        return self

    @classmethod
    async def join_from_share(cls, api, rc, share_string: str):
        """Join an existing directory from a DIR: share string."""
        log.info("Joining directory from share string...")
        self = cls()
        self._db = await api.open_table_db("veilid_irc_directory", 1)

        payload = share_string
        if payload.upper().startswith("DIR:"):
            payload = payload[4:]

        info = json.loads(base64.b64decode(payload).decode())
        self.dir_key = veilid.RecordKey(info["k"])
        self.dir_keypair = veilid.KeyPair(info["p"])

        log.debug("Opening directory record: %s", self.dir_key)
        await rc.open_dht_record(self.dir_key, writer=self.dir_keypair)

        await self._db.store(b"dir_key", str(self.dir_key).encode())
        await self._db.store(b"dir_keypair", str(self.dir_keypair).encode())
        log.info("Joined directory: %s", self.dir_key)

        return self

    def get_share_string(self) -> str:
        """Return the DIR: share string for this directory."""
        info = {"k": str(self.dir_key), "p": str(self.dir_keypair)}
        encoded = base64.b64encode(json.dumps(info).encode()).decode()
        return f"DIR:{encoded}"

    # ------------------------------------------------------------------
    # Publish / unpublish
    # ------------------------------------------------------------------

    async def publish_channel(
        self, rc, nick: str, name: str, topic: str,
        share_string: str, members: int = 0
    ) -> str:
        """Publish a channel to the directory.  Returns the 4-char short code.

        If the channel is already published, update the existing entry.
        Uses cached DHT reads (force=False) to avoid network stalls.
        """
        short = _generate_short_code(share_string)
        log.info("Publishing channel %s (code=%s) by %s", name, short, nick)

        entry = {
            "nick": nick,
            "name": name,
            "topic": topic,
            "share": share_string,
            "short": short,
            "members": members,
            "ts": time.time(),
        }
        data = json.dumps(entry).encode()

        # First pass: look for existing entry for this channel (update in place)
        log.debug("Pass 1: scanning for existing entry to update...")
        for subkey in range(1, self.MAX_SLOTS + 1):
            try:
                vd = await _timed_get(rc, self.dir_key, subkey, force=False)
                if vd is None:
                    continue
                existing = json.loads(vd.data.decode())
                if existing.get("share") == share_string:
                    log.info("  Found existing at subkey %d, updating", subkey)
                    ok = await _timed_set(rc, self.dir_key, subkey, data, self.dir_keypair)
                    if ok:
                        log.info("Published (updated) %s code=%s", name, short)
                        return short
                    log.warning("  Write failed at subkey %d, trying next", subkey)
            except Exception as e:
                log.debug("  subkey %d: error %s", subkey, e)
                continue

        # Second pass: find an empty or stale slot
        log.debug("Pass 2: scanning for empty/stale slot...")
        now = time.time()
        for subkey in range(1, self.MAX_SLOTS + 1):
            vd = await _timed_get(rc, self.dir_key, subkey, force=False)

            if vd is None or vd.data == b"" or vd.data == b"{}":
                log.info("  Empty slot at subkey %d, writing", subkey)
                ok = await _timed_set(rc, self.dir_key, subkey, data, self.dir_keypair)
                if ok:
                    log.info("Published %s at subkey %d, code=%s", name, subkey, short)
                    return short
                continue

            try:
                existing = json.loads(vd.data.decode())
                if not existing.get("share"):
                    log.info("  Cleared slot at subkey %d, writing", subkey)
                    ok = await _timed_set(rc, self.dir_key, subkey, data, self.dir_keypair)
                    if ok:
                        return short
                    continue
                age = now - existing.get("ts", 0)
                if age > STALE_AGE:
                    log.info("  Stale slot at subkey %d (age=%.0fs), reclaiming",
                             subkey, age)
                    ok = await _timed_set(rc, self.dir_key, subkey, data, self.dir_keypair)
                    if ok:
                        return short
                    continue
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                log.info("  Corrupted slot at subkey %d (%s), reclaiming", subkey, e)
                ok = await _timed_set(rc, self.dir_key, subkey, data, self.dir_keypair)
                if ok:
                    return short
                continue

        log.error("Directory full! All %d slots occupied", self.MAX_SLOTS)
        raise RuntimeError(f"Directory full (all {self.MAX_SLOTS} slots occupied)")

    async def unpublish_channel(self, rc, share_string: str):
        """Remove a channel from the directory."""
        log.info("Unpublishing channel with share=%s...", share_string[:30])
        for subkey in range(1, self.MAX_SLOTS + 1):
            try:
                vd = await _timed_get(rc, self.dir_key, subkey, force=False)
                if vd is None:
                    continue
                existing = json.loads(vd.data.decode())
                if existing.get("share") == share_string:
                    ok = await _timed_set(
                        rc, self.dir_key, subkey,
                        json.dumps({"share": None}).encode(), self.dir_keypair,
                    )
                    if ok:
                        log.info("Unpublished channel from subkey %d", subkey)
                    return ok
            except Exception:
                continue
        log.debug("Channel not found in directory for unpublish")
        return False

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def list_channels(self, rc) -> list[dict]:
        """Fetch all active channel entries from the directory."""
        log.debug("Listing channels from directory...")
        channels = []
        for subkey in range(1, self.MAX_SLOTS + 1):
            try:
                vd = await _timed_get(rc, self.dir_key, subkey, force=False)
                if vd is None:
                    continue
                entry = json.loads(vd.data.decode())
                if entry.get("share"):
                    channels.append(entry)
            except Exception as e:
                log.debug("  subkey %d: error %s", subkey, e)
                continue
        log.info("Listed %d channels from directory", len(channels))
        return channels

    async def find_by_short_code(self, rc, code: str) -> dict | None:
        """Look up a channel by its 4-char short code."""
        code = code.upper()
        log.debug("Looking up short code: %s", code)
        channels = await self.list_channels(rc)
        for entry in channels:
            if entry.get("short", "").upper() == code:
                log.info("Found channel for code %s: %s", code, entry.get("name"))
                return entry
        log.debug("Short code %s not found", code)
        return None

    async def find_by_name(self, rc, name: str) -> dict | None:
        """Look up a channel by name (e.g. #general)."""
        name = name.lower()
        if not name.startswith("#"):
            name = "#" + name
        log.debug("Looking up channel by name: %s", name)
        channels = await self.list_channels(rc)
        for entry in channels:
            if entry.get("name", "").lower() == name:
                log.info("Found channel: %s", entry.get("name"))
                return entry
        log.debug("Channel %s not found in directory", name)
        return None
