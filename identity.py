"""Persistent user identity via Veilid DHT + local TableDb storage.

Profile DHT record (veilid.DHTSchema.dflt(2)):
  subkey 0: {"handle": "alice", "status": "available", "v": 1}
  subkey 1: {"room": "VLD0:xxx...", "ts": 1234567890} or {"room": null}

Local persistence via TableDb("ascii_chat_identity", 1):
  Keys: profile_key, owner_keypair, handle
"""

import json
import time

import veilid


class Identity:
    """Manages a user's persistent identity on the Veilid network."""

    def __init__(self):
        self.profile_key = None       # TypedKey of our profile DHT record
        self.owner_keypair = None      # KeyPair for writing to profile record
        self.handle = None             # Our display name
        self._db = None                # TableDb handle

    @classmethod
    async def load_or_create(cls, api, rc, default_handle=None):
        """Load existing identity from TableDb or create a new one."""
        self = cls()
        self._db = await api.open_table_db("ascii_chat_identity", 1)

        # Try to load existing identity
        raw_key = await self._db.load(b"profile_key")
        raw_kp = await self._db.load(b"owner_keypair")
        raw_handle = await self._db.load(b"handle")

        if raw_key and raw_kp:
            self.profile_key = veilid.RecordKey(raw_key.decode())
            self.owner_keypair = veilid.KeyPair(raw_kp.decode())
            self.handle = raw_handle.decode() if raw_handle else "anon"

            # Re-open the existing DHT record with our keypair for write access
            try:
                await rc.open_dht_record(self.profile_key, writer=self.owner_keypair)
            except Exception:
                # Record may have expired; clear stale data and create fresh
                await self._db.store(b"profile_key", b"")
                await self._db.store(b"owner_keypair", b"")
                await self._db.store(b"handle", b"")
                await self._create_new(api, rc, default_handle or self.handle)
        else:
            await self._create_new(api, rc, default_handle or "anon")

        return self

    async def _create_new(self, api, rc, handle):
        """Create a fresh profile DHT record and persist locally."""
        schema = veilid.DHTSchema.dflt(2)
        record = await rc.create_dht_record(veilid.CryptoKind.CRYPTO_KIND_VLD0, schema)
        self.profile_key = record.key
        self.owner_keypair = record.owner_key_pair()
        self.handle = handle

        # Write initial profile to subkey 0
        profile = {"handle": self.handle, "status": "available", "v": 1}
        await rc.set_dht_value(
            self.profile_key,
            veilid.ValueSubkey(0),
            json.dumps(profile).encode(),
        )

        # Write empty room to subkey 1
        await rc.set_dht_value(
            self.profile_key,
            veilid.ValueSubkey(1),
            json.dumps({"room": None}).encode(),
        )

        # Persist to TableDb
        await self._db.store(b"profile_key", str(self.profile_key).encode())
        await self._db.store(b"owner_keypair", str(self.owner_keypair).encode())
        await self._db.store(b"handle", self.handle.encode())

    async def set_handle(self, rc, name):
        """Update handle locally and on DHT."""
        self.handle = name
        await self._db.store(b"handle", name.encode())

        profile = {"handle": self.handle, "status": "available", "v": 1}
        await rc.set_dht_value(
            self.profile_key,
            veilid.ValueSubkey(0),
            json.dumps(profile).encode(),
        )

    async def publish_room(self, rc, room_key):
        """Write current room key to profile subkey 1 (or null to clear)."""
        room_str = str(room_key) if room_key else None
        data = {"room": room_str, "ts": time.time()}
        await rc.set_dht_value(
            self.profile_key,
            veilid.ValueSubkey(1),
            json.dumps(data).encode(),
        )

    @staticmethod
    async def lookup_profile(rc, profile_key_str):
        """Read another user's profile + room from DHT.

        Returns (handle, status, room_key_str_or_None) or None on failure.
        """
        try:
            key = veilid.RecordKey(profile_key_str)
            await rc.open_dht_record(key)

            profile = None
            vd = await rc.get_dht_value(key, veilid.ValueSubkey(0), True)
            if vd:
                profile = json.loads(vd.data.decode())

            room_info = None
            vd = await rc.get_dht_value(key, veilid.ValueSubkey(1), True)
            if vd:
                room_info = json.loads(vd.data.decode())

            await rc.close_dht_record(key)

            if not profile:
                return None

            handle = profile.get("handle", "anon")
            status = profile.get("status", "unknown")
            room = room_info.get("room") if room_info else None
            return handle, status, room
        except Exception:
            return None
