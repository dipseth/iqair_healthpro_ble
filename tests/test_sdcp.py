"""Protocol tests against frames captured from a live HealthPro Plus (2026-10-21)."""

import importlib.util
import pathlib
import sys

import pytest

# Load sdcp.py directly so the tests don't need Home Assistant installed.
_path = pathlib.Path(__file__).parents[1] / "custom_components/iqair_healthpro/sdcp.py"
_spec = importlib.util.spec_from_file_location("sdcp", _path)
sdcp = importlib.util.module_from_spec(_spec)
sys.modules["sdcp"] = sdcp  # dataclasses look the module up
_spec.loader.exec_module(sdcp)


def test_crc_check_value():
    assert sdcp.crc16(b"123456789") == 0x29B1


def test_build_connect_frame_matches_capture():
    frame = sdcp.build_frame(sdcp.CW_CONNECT, sdcp.connect_payload(0x6AD85AFE))
    assert frame.hex() == "010a0002960000fe5ad86afcdf"


def test_build_write_frame_matches_capture():
    frame = sdcp.build_frame(sdcp.CW_DP_WRITE, sdcp.write_payload(sdcp.DP_LIGHT, b"\x00"))
    assert frame.hex() == "1405009d13002a5d"


def test_reassemble_and_decode_split_response():
    ra = sdcp.Reassembler()
    parts = [
        "93230000cf0b02000200d00b02000200d10b0200",
        "0200c50b02000d03d60b040082000000afd0",
    ]
    assert ra.feed(bytes.fromhex(parts[0])) == []
    (raw,) = ra.feed(bytes.fromhex(parts[1]))
    f = sdcp.parse_frame(raw)
    assert f.crc_ok and f.cw == 0x93 and f.status == sdcp.STATUS_OK
    state = sdcp.decode_state(sdcp.decode_tlv(f.payload))
    assert state == {"pm25": 2, "pm1": 2, "pm10": 2, "fan_rpm": 781, "cadr": 130}


def test_bad_crc_detected():
    raw = bytearray.fromhex("810700000214200124f6")
    assert sdcp.parse_frame(bytes(raw)).crc_ok
    raw[5] ^= 1
    assert not sdcp.parse_frame(bytes(raw)).crc_ok


def test_decode_controls_and_filters():
    raw = {
        sdcp.DP_STANDBY: b"\x00", sdcp.DP_AUTO: b"\x01", sdcp.DP_LIGHT: b"\x00",
        sdcp.DP_FAN_LEVEL: b"\x02", sdcp.DP_WIFI_RSSI: b"\xc4",
        sdcp.DP_FILTER_LIFE: bytes([96, 96, 98, 0]),
        sdcp.DP_PRODUCT_NAME: b"HealthPro Plus L002-4",
    }
    s = sdcp.decode_state(raw)
    assert s["power"] is True and s["auto"] is True and s["light"] is False
    assert s["fan_level"] == 2 and s["wifi_rssi"] == -60
    assert (s["filter_pre"], s["filter_gas"], s["filter_main"]) == (96, 96, 98)
    assert s["product_name"] == "HealthPro Plus L002-4"


def test_truncated_tlv_raises():
    with pytest.raises(sdcp.SdcpError):
        sdcp.decode_tlv(bytes.fromhex("cf0b0400aa"))


def test_level_for_percent_snaps_to_device_table():
    levels = (22, 37, 52, 62, 71, 98)
    assert sdcp.level_for_percent(98, levels) == 6
    assert sdcp.level_for_percent(22, levels) == 1
    assert sdcp.level_for_percent(0, levels) == 1
    assert sdcp.level_for_percent(66, levels) == 4
    assert sdcp.level_for_percent(57, levels) == 4  # exact tie rounds up
    assert sdcp.level_for_percent(98, None) == 6  # table not read yet -> defaults
