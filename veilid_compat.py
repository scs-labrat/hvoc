"""Compatibility shim for Veilid Python API type imports.

Different versions of the veilid package export types at different
locations and have different method signatures.  This module tries
multiple paths so the app works with both the pip release and
source-built versions.

Usage:
    from veilid_compat import TypedKey, KeyPair, ValueSubkey, CryptoKind, DHTSchema
    from veilid_compat import set_dht_value  # compat wrapper
"""

import veilid

from irc_log import get_logger

log = get_logger(__name__)


def _resolve(name: str):
    """Find a veilid type by name, trying multiple locations."""
    # 1. Top-level: veilid.TypedKey
    obj = getattr(veilid, name, None)
    if obj is not None:
        return obj

    # 2. veilid.types.TypedKey
    types_mod = getattr(veilid, "types", None)
    if types_mod:
        obj = getattr(types_mod, name, None)
        if obj is not None:
            return obj

    # 3. veilid.json_api.TypedKey
    json_mod = getattr(veilid, "json_api", None)
    if json_mod:
        obj = getattr(json_mod, name, None)
        if obj is not None:
            return obj

    raise ImportError(
        f"Cannot find veilid.{name} in any known location. "
        f"Your veilid package may be too old or too new."
    )


TypedKey = _resolve("TypedKey")
KeyPair = _resolve("KeyPair")
ValueSubkey = _resolve("ValueSubkey")
CryptoKind = _resolve("CryptoKind")
DHTSchema = _resolve("DHTSchema")

# SetDHTValueOptions may or may not exist depending on API version
SetDHTValueOptions = None
try:
    SetDHTValueOptions = _resolve("SetDHTValueOptions")
except ImportError:
    pass


async def set_dht_value(rc, key, subkey: int, data: bytes, writer_keypair):
    """Write a DHT value with the correct API for the installed veilid version.

    Handles two API variants:
      - v0.5.x (source): rc.set_dht_value(key, subkey, data, options=SetDHTValueOptions(writer=kp))
      - v0.4.x (pip):    rc.set_dht_value(key, subkey, data, writer=kp)
    """
    vs = ValueSubkey(subkey)

    if SetDHTValueOptions is not None:
        opts = SetDHTValueOptions(writer=writer_keypair)
        return await rc.set_dht_value(key, vs, data, options=opts)
    else:
        return await rc.set_dht_value(key, vs, data, writer=writer_keypair)

