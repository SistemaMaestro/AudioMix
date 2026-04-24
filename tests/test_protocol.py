"""Unit tests for the UCNET packet codec (studiolive/protocol.py)."""
import struct
import pytest

from studiolive.protocol import (
    HEADER, CBYTES,
    pack, unpack, drain_packets,
    keepalive_packet, subscribe_packet, ftbr_probe_packet,
    pv_float_packet, set_volume_packet, set_mute_packet,
)


class TestPackUnpack:
    def test_round_trip_empty(self):
        pkt = pack("KA", b"")
        code, data = unpack(pkt)
        assert code == "KA"
        assert data == b""

    def test_round_trip_with_data(self):
        payload = b"\x01\x02\x03\x04"
        pkt = pack("PV", payload)
        code, data = unpack(pkt)
        assert code == "PV"
        assert data == payload

    def test_header_present(self):
        pkt = pack("MS", b"hello")
        assert pkt[:4] == HEADER

    def test_cbytes_present(self):
        pkt = pack("JM", b"")
        assert pkt[8:12] == CBYTES

    def test_payload_len_field(self):
        data = b"abc"
        pkt = pack("ZB", data)
        payload_len = struct.unpack("<H", pkt[4:6])[0]
        # payload_len = 2 (code) + 4 (cbytes) + len(data)
        assert payload_len == 2 + 4 + len(data)

    def test_invalid_code_length(self):
        with pytest.raises(ValueError):
            pack("KAA", b"")

    def test_unpack_garbage(self):
        code, data = unpack(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08")
        assert code is None
        assert data is None


class TestDrainPackets:
    def test_single_packet(self):
        pkt = pack("KA", b"")
        packets, leftover = drain_packets(pkt)
        assert len(packets) == 1
        assert packets[0][0] == "KA"
        assert leftover == b""

    def test_two_packets(self):
        p1 = pack("KA", b"")
        p2 = pack("PV", b"\x01\x02")
        packets, leftover = drain_packets(p1 + p2)
        assert len(packets) == 2
        assert packets[0][0] == "KA"
        assert packets[1][0] == "PV"
        assert packets[1][1] == b"\x01\x02"

    def test_incomplete_packet(self):
        pkt = pack("KA", b"")
        # Strip last byte → incomplete
        packets, leftover = drain_packets(pkt[:-1])
        assert packets == []
        assert len(leftover) > 0

    def test_garbage_prefix_is_skipped(self):
        garbage = b"\xDE\xAD\xBE\xEF"
        pkt = pack("MS", b"data")
        packets, leftover = drain_packets(garbage + pkt)
        assert len(packets) == 1
        assert packets[0][0] == "MS"

    def test_empty_buffer(self):
        packets, leftover = drain_packets(b"")
        assert packets == []
        assert leftover == b""


class TestHighLevelPackets:
    def test_keepalive_code(self):
        code, _ = unpack(keepalive_packet())
        assert code == "KA"

    def test_subscribe_code(self):
        code, _ = unpack(subscribe_packet())
        assert code == "JM"

    def test_ftbr_probe_code(self):
        code, data = unpack(ftbr_probe_packet(1))
        assert code == "FR"
        # payload starts with uint16 BE req_id, then b"Ftbr"
        assert data[2:6] == b"Ftbr"

    def test_ftbr_req_id_encoding(self):
        for req_id in (0, 1, 255, 1000, 65535):
            _, data = unpack(ftbr_probe_packet(req_id))
            decoded_id = struct.unpack(">H", data[:2])[0]
            assert decoded_id == req_id & 0xFFFF

    def test_pv_float_path_termination(self):
        _, data = unpack(pv_float_packet("line/ch1/volume", 0.72))
        path_bytes, _, value_bytes = data.partition(b"\x00\x00\x00")
        assert path_bytes == b"line/ch1/volume"
        value = struct.unpack("<f", value_bytes)[0]
        assert abs(value - 0.72) < 1e-6

    def test_set_volume_clamps(self):
        # level > 100 should clamp to 100
        code, data = unpack(set_volume_packet("line", 1, 150))
        assert code == "PV"
        value = struct.unpack("<f", data[-4:])[0]
        assert abs(value - 1.0) < 1e-6

    def test_set_volume_zero(self):
        _, data = unpack(set_volume_packet("aux", 3, 0))
        value = struct.unpack("<f", data[-4:])[0]
        assert abs(value - 0.0) < 1e-6

    def test_set_mute_true(self):
        _, data = unpack(set_mute_packet("line", 5, True))
        value = struct.unpack("<f", data[-4:])[0]
        assert abs(value - 1.0) < 1e-6

    def test_set_mute_false(self):
        _, data = unpack(set_mute_packet("line", 5, False))
        value = struct.unpack("<f", data[-4:])[0]
        assert abs(value - 0.0) < 1e-6
