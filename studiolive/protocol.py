"""
PreSonus StudioLive III (UCNET) packet codec.

Reverse-engineered — references:
  https://github.com/featherbear/presonus-studiolive-api
  https://featherbear.cc/presonus-studiolive-api/

Packet layout:
    0..3   header       b"UC\\x00\\x01"
    4..5   payload_len  uint16 LE = 2 (code) + 4 (cbytes) + len(data)
    6..7   code         ASCII 2 chars (e.g. "KA", "JM", "PV", "MS", "ZB"...)
    8..11  cbytes       [A, 0x00, B, 0x00]  A=0x68, B=0x65
    12..   data         payload
"""
import struct
import json

HEADER = b"UC\x00\x01"
CBYTES = bytes([0x68, 0x00, 0x65, 0x00])
DISCOVERY_PORT = 47809
CONTROL_PORT = 53000


def pack(code: str, data: bytes = b"") -> bytes:
    if len(code) != 2:
        raise ValueError("code must be 2 ASCII chars")
    payload_len = 2 + 4 + len(data)
    return HEADER + struct.pack("<H", payload_len) + code.encode("ascii") + CBYTES + data


def unpack(packet: bytes):
    """Return (code, data) or (None, None) if not a valid packet."""
    if len(packet) < 12 or packet[:4] != HEADER:
        return None, None
    payload_len = struct.unpack("<H", packet[4:6])[0]
    code = packet[6:8].decode("ascii", errors="replace")
    data = packet[12:6 + payload_len] if (payload_len + 6) <= len(packet) else packet[12:]
    return code, data


def iter_packets(buf: bytes):
    """Yield (code, data, consumed_bytes) from a stream buffer; stops at incomplete packet."""
    i = 0
    while i + 6 <= len(buf):
        if buf[i:i + 4] != HEADER:
            # resync to next header
            nxt = buf.find(HEADER, i + 1)
            if nxt == -1:
                break
            i = nxt
            continue
        payload_len = struct.unpack("<H", buf[i + 4:i + 6])[0]
        total = 6 + payload_len
        if i + total > len(buf):
            break
        pkt = buf[i:i + total]
        code, data = unpack(pkt)
        yield code, data
        i += total
    return i  # caller can't read generator return directly; use drain_packets


def drain_packets(buf: bytes):
    """Parse as many complete packets as possible; return (list_of_(code,data), leftover)."""
    out = []
    i = 0
    while i + 6 <= len(buf):
        if buf[i:i + 4] != HEADER:
            nxt = buf.find(HEADER, i + 1)
            if nxt == -1:
                i = len(buf)
                break
            i = nxt
            continue
        payload_len = struct.unpack("<H", buf[i + 4:i + 6])[0]
        total = 6 + payload_len
        if i + total > len(buf):
            break
        code, data = unpack(buf[i:i + total])
        out.append((code, data))
        i += total
    return out, buf[i:]


# ---- Payload builders ----

def json_payload(obj) -> bytes:
    """JM payload: len16LE + 0x00 0x00 + JSON string (stringified with single space indent)."""
    js = json.dumps(obj, indent=" ").encode("utf-8")
    return struct.pack("<H", len(js)) + b"\x00\x00" + js


def subscribe_packet() -> bytes:
    """JM Subscribe — triggers ZB state dump + SubscriptionReply from mixer."""
    payload = json_payload({
        "id": "Subscribe",
        "clientName": "UC-Surface",
        "clientInternalName": "ucremoteapp",
        "clientType": "StudioLive API",
        "clientDescription": "PyHelloWorld",
        "clientIdentifier": "133d066a919ea0ea",
        "clientOptions": "perm users levl redu rtan",
        "clientEncoding": 23106,
    })
    return pack("JM", payload)


def unsubscribe_packet() -> bytes:
    return pack("JM", json_payload({"id": "Unsubscribe"}))


def keepalive_packet() -> bytes:
    return pack("KA")


def ftbr_probe_packet(req_id: int) -> bytes:
    """FR (FileRequest) 'Ftbr' probe sent alongside KA. Mixer replies with FD;
    featherbear's client uses it as a health check. id is uint16 BE (note: BE, not LE)."""
    import struct as _s
    payload = _s.pack(">H", req_id & 0xFFFF) + b"Ftbr" + b"\x00\x00"
    return pack("FR", payload)


def pv_float_packet(path: str, value: float) -> bytes:
    """Parameter-Value packet: path\\x00\\x00\\x00 + float32 LE."""
    payload = path.encode("ascii") + b"\x00\x00\x00" + struct.pack("<f", float(value))
    return pack("PV", payload)


# ---- High-level helpers (channel paths) ----
# Channel type prefixes: line, aux, fxbus, return, fxreturn, talkback, sub, main,
# filtergroup (DCA), mono, master (64S). Channels are 1-indexed.

def channel_base(ch_type: str, channel: int) -> str:
    if ch_type in ("main", "talkback"):
        channel = 1
    return f"{ch_type}/ch{int(channel)}"


def set_volume_packet(ch_type: str, channel: int, level_0_100: float) -> bytes:
    """
    Linear level [0..100] where 72≈unity (0 dB), 100=+10 dB, 0=-84 dB.
    Path: <type>/ch<n>/volume for main mix. Aux sends use a different path; see setLevel in TS lib.
    """
    base = channel_base(ch_type, channel)
    path = f"{base}/volume"
    return pv_float_packet(path, max(0.0, min(100.0, level_0_100)) / 100.0)


def set_mute_packet(ch_type: str, channel: int, muted: bool) -> bytes:
    base = channel_base(ch_type, channel)
    return pv_float_packet(f"{base}/mute", 1.0 if muted else 0.0)
