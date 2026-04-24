"""
UBJSON deserializer used by PreSonus StudioLive III state dumps.

Based on featherbear's partial implementation (src/lib/util/zlib/ubjson.ts).
Only the subset the console actually emits is handled.

Type markers:
  { } new dict,  [ ] new array
  S   string (prefix 'i' + uint8 length)
  d   float32 BE
  i   int8
  U   uint8
  l   int32 BE
  L   int64 BE
Key prefix inside a dict: 'i' + uint8 length + utf-8 key bytes.
"""
import struct


def deserialize(buf: bytes):
    if not buf or buf[0] != 0x7B:  # '{'
        raise ValueError("UBJSON must start with '{'")
    idx = 1
    root: dict = {}
    working: list = [root]

    while idx < len(buf):
        container = working[0]
        # Close current container, or read next key
        if isinstance(container, list):
            if buf[idx] == 0x5D:  # ']'
                idx += 1
                working.pop(0)
                continue
            key = None
        else:
            c = buf[idx]
            idx += 1
            if c == 0x7D:  # '}'
                working.pop(0)
                continue
            if c != 0x69:  # 'i' (key length prefix)
                raise ValueError(f"expected 'i' key marker, got 0x{c:02x} at {idx - 1}")
            klen = buf[idx]
            idx += 1
            key = buf[idx:idx + klen].decode("utf-8", errors="replace")
            idx += klen

        t = buf[idx]
        idx += 1

        if t == 0x7B:  # '{' new dict
            leaf: dict = {}
            _attach(container, key, leaf)
            working.insert(0, leaf)
            continue
        if t == 0x5B:  # '[' new array
            leaf2: list = []
            _attach(container, key, leaf2)
            working.insert(0, leaf2)
            continue

        if t == 0x53:  # 'S' string
            if buf[idx] != 0x69:
                raise ValueError("expected 'i' string-length marker")
            idx += 1
            slen = buf[idx]
            idx += 1
            value = buf[idx:idx + slen].decode("utf-8", errors="replace")
            idx += slen
        elif t == 0x64:  # 'd' float32 BE
            value = struct.unpack_from(">f", buf, idx)[0]
            idx += 4
        elif t == 0x69:  # 'i' int8
            value = struct.unpack_from(">b", buf, idx)[0]
            idx += 1
        elif t == 0x55:  # 'U' uint8
            value = buf[idx]
            idx += 1
        elif t == 0x6C:  # 'l' int32 BE
            value = struct.unpack_from(">i", buf, idx)[0]
            idx += 4
        elif t == 0x4C:  # 'L' int64 BE
            value = struct.unpack_from(">q", buf, idx)[0]
            idx += 8
        else:
            raise ValueError(f"unknown UBJSON type 0x{t:02x} at {idx - 1}")

        _attach(container, key, value)

    return root


def _attach(container, key, value):
    if isinstance(container, list):
        container.append(value)
    else:
        container[key] = value
