"""UDP discovery listener — StudioLive III consoles broadcast every ~3s to 255.255.255.255:47809."""
import asyncio
import socket
from . import protocol


async def discover(timeout: float = 6.0):
    """Listen for discovery broadcasts; return list of dicts {ip, name, serial}."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    try:
        # On Windows, SO_REUSEPORT may not exist; SO_REUSEADDR is enough for broadcast receive.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", protocol.DISCOVERY_PORT))
    except OSError as e:
        sock.close()
        raise RuntimeError(f"Could not bind UDP {protocol.DISCOVERY_PORT}: {e}")
    sock.setblocking(False)

    # serial -> best candidate. Prefer the broadcast whose source port == CONTROL_PORT (the
    # actual console) over rebroadcasts emitted by Universal Control on loopback / VM NICs.
    found: dict[str, dict] = {}
    end = loop.time() + timeout
    try:
        while loop.time() < end:
            remaining = max(0.1, end - loop.time())
            try:
                data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except OSError:
                continue
            code, payload = protocol.unpack(data)
            if code is None or len(payload) < 20:
                continue
            fragments = payload[20:].split(b"\x00")
            strs = [f.decode("utf-8", errors="replace") for f in fragments if f]
            name = strs[0] if len(strs) > 0 else ""
            serial = strs[2] if len(strs) > 2 else ""
            if not serial:
                continue
            is_console = addr[1] == protocol.CONTROL_PORT  # source port 53000 = real console
            is_loopback = addr[0].startswith("127.")
            cand = {
                "ip": addr[0],
                "port": addr[1],
                "name": name,
                "serial": serial,
                "is_console": is_console,
                "is_loopback": is_loopback,
            }
            prev = found.get(serial)
            # Prefer console broadcast; otherwise prefer non-loopback.
            if (prev is None
                or (cand["is_console"] and not prev["is_console"])
                or (not prev["is_console"] and prev["is_loopback"] and not cand["is_loopback"])):
                found[serial] = cand
    finally:
        sock.close()
    return list(found.values())
