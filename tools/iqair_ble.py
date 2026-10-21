#!/usr/bin/env python3
"""iqair_ble.py — standalone BLE client for the IQAir HealthPro Plus XE, speaking
SDCP (IQAir "Smart Device Communication Protocol") directly. No app, no cloud.

Protocol details: docs/PROTOCOL.md. Payloads are raw bytes / TLV.

Frame (serializer 0x1007bde30 / deserializer 0x1007bd9f4):
    request : [cw][len u16 LE][payload][crc16 LE]          len = payload + 2
    response: [cw|0x80][len u16 LE][status][payload][crc16 LE]   len = 1 + payload + 2
    crc16 = CRC-16/CCITT-FALSE over every byte before the CRC; stored LITTLE-endian.
    App writes with-response, chunked to <= 20 bytes.
Status byte: 0x00 ok, 0x80 morePacketsFollow, 0xff unsolicited, 0xfe pending,
    0x01..0x0a = error,busy,timeout,accessDenied,invalidRange,invalidSize,overflow,
                 invalidCmd,invalidCrc,preconditionFailed.
Control words: 01 connect, 02 disconnect, 03 productInformation, 04 accessPermissions,
    10 dpDefinitionsRead, 11 dpDefinitionsReadAll, 12 dpRead, 13 dpReadList,
    14 dpWrite, 15 dpReadAll, 16 dpWriteList, 20 restart, 21 fwUpdate,
    22 restoreDefaults, 31 virtualStorageWrite.   (response = cw | 0x80)
Payloads:
    connect   (0x01): 02 96 00 00 + u32 LE epoch seconds   (fn 0x100771d74)
              -> 0x81 reply [protoVer u8][rxBufSize u16 LE] sets sdcpConnected
    readList  (0x13): concat of u16 LE dataPoint ids        (fn 0x1007f77e0)
    write     (0x14): u16 LE id + value bytes               (fn 0x1007f6f80..)
    writeList (0x16): repeat [id u16 LE][len u16 LE][value] (fn 0x100572764)

GATT:  service 1b5ae7e4-...  TX 55340670-...1f64 (write)  RX 6f5e9f58-...94b6 (notify)
The machine must already be paired with the purifier (once, with the unit's code).
Needs: pip install bleak

Usage:
  python tools/iqair_ble.py --read                 # connect + read sensors
  python tools/iqair_ble.py --read --ids all       # every known dataPoint
  python tools/iqair_ble.py --connect-only         # just the 0x01/0x81 handshake
  python tools/iqair_ble.py --send 15              # raw cw + optional --payload hex
  python tools/iqair_ble.py --listen 30            # passive listen
  python tools/iqair_ble.py --set light=off        # write + read back
  python tools/iqair_ble.py --set auto=off fan=L3  # several, in order
  python tools/iqair_ble.py --set power=off --write-mode list   # use 0x16
"""
import argparse, asyncio, sys, time

SVC = "1b5ae7e4-f469-440f-a0b4-aed74acd94f8"
TX  = "55340670-4e1c-471a-bd05-1891775a1f64"   # app -> device (write)
RX  = "6f5e9f58-ed60-47a2-bbe4-ec93545b94b6"   # device -> app (notify)
NAME_HINT = "IQAir"
CHUNK = 20

# ---- DataPoint ids (tables 0x101242d02/0x101242d53, names from reflection strings) ----
DATAPOINTS = {
    0x03e8: "purifier_serialnumber", 0x03ea: "purifier_productname",
    0x03eb: "purifier_purifiercolor", 0x03ed: "purifier_applfwver",
    0x03ef: "purifier_applfwcrc", 0x03f3: "purifier_hwver",
    0x03f4: "purifier_bootloaderfwver", 0x03f5: "purifier_producttype",
    0x03f6: "purifier_productvariation", 0x03f7: "purifier_producttechrev",
    0x03fe: "purifier_commchipfwver", 0x03ff: "purifier_applfwnvm",
    0x0400: "purifier_commchipfwnvm", 0x0401: "purifier_certificatever",
    0x0402: "purifier_certificatevernvm", 0x0406: "purifier_ethernetsupported",
    0x0410: "purifier_registrnr", 0x044c: "purifier_netwip",
    0x044d: "purifier_netwnetmask", 0x044e: "purifier_netwgateway",
    0x044f: "purifier_netwinterface", 0x0450: "settings_netwinterfaceenabled",
    0x07cf: "purifier_producterrors", 0x07d1: "filter_filtertype",
    0x07d2: "filter_filtervariation", 0x07d5: "filter_filterstatus",
    0x07db: "filter_filterbid", 0x0bb8: "sensor_ambienttemperature",
    0x0bb9: "sensor_ambienthumidity", 0x0bc5: "sensor_fanrpm",
    0x0bcd: "settings_bletxpower", 0x0bcf: "sensor_pm25", 0x0bd0: "sensor_pm1",
    0x0bd1: "sensor_pm10", 0x0bd6: "sensor_cadr", 0x0fc3: "settings_timezone",
    0x0fc4: "settings_daylightsavingtime", 0x0fc5: "settings_syncutc",
    0x0fc6: "settings_calendarenable", 0x0fc7: "settings_calendartempoverride",
    0x0fc8: "settings_fanamprofiles", 0x0fc9: "settings_fanamprofileidx",
    0x0fdc: "feature_set", 0x1004: "wifi_wificmdstat", 0x1005: "wifi_wifinetw",
    0x1006: "wifi_wifipsw", 0x1008: "wifi_wifimacaddress", 0x100c: "wifi_wifiapssid",
    0x100d: "wifi_wifiapmac", 0x100e: "wifi_wifirssi", 0x1018: "purifier_ethmacaddress",
    0x1068: "cloud_cloudurl", 0x1069: "cloud_cloudport", 0x106a: "cloud_sdcpservaddr",
    0x106b: "cloud_sdcpservport", 0x106c: "cloud_sdcpservconmode",
    0x106d: "cloud_remconstat", 0x10cc: "cloud_sntpserv1",
    0x1388: "ui_fanspeedvalue", 0x1392: "ui_fanspeedmanual", 0x139c: "ui_uistandby",
    0x139d: "ui_uilight", 0x139e: "ui_uiauto", 0x139f: "ui_uiradio",
    0x13a1: "ui_uikeylocked", 0x13a2: "ui_uiidentifyme", 0x13a3: "ui_uiradiolocked",
    0x13a6: "ui_ledbrighthigh", 0x13a7: "ui_ledbrightlow", 0x13a8: "ui_leddimhigh",
    0x13a9: "ui_leddimlowstandby", 0x13aa: "ui_ledbrightaccent",
    0x1770: "usage_tottimeonpower", 0x1771: "usage_tottimefanrun",
    0x1772: "usage_totcumairvolume", 0x753f: "internals_blemacaddress",
    0x823f: "fwureflashmask", 0x9c49: "internals_localtimesec",
    0x9c4a: "internals_lastpoweroff", 0x9c4b: "internals_localtimestartup",
    0x9c4d: "internals_iqlogger",
    # not in the app's name table; identified live (2026-10-21)
    0xc73e: "x_filterlife_pct", 0x13a5: "x_fanspeed_levels_pct", 0x138b: "x_fanspeed_level",
}
# The Home Assistant-relevant subset; wifi_wifipsw deliberately excluded everywhere.
SENSOR_IDS = [0x0bcf, 0x0bd0, 0x0bd1, 0x0bb8, 0x0bb9, 0x0bc5, 0x0bd6, 0x07d5, 0x07cf,
              0x1388, 0x1392, 0x139c, 0x139e, 0x1770, 0x1771, 0x1772, 0x03ea, 0x03ed]
SECRET_IDS = {0x1006}

STATUS = {0x00: "ok", 0x80: "morePacketsFollow", 0xff: "unsolicited", 0xfe: "pending",
          0x01: "error", 0x02: "busy", 0x03: "timeout", 0x04: "accessDenied",
          0x05: "invalidRange", 0x06: "invalidSize", 0x07: "overflow",
          0x08: "invalidCmd", 0x09: "invalidCrc", 0x0a: "preconditionFailed"}

# ---- CRC-16/CCITT-FALSE (recovered from re/AirVisual.bin) --------------------
_CRC_TABLE = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) if (_c & 0x8000) else (_c << 1)
        _c &= 0xFFFF
    _CRC_TABLE.append(_c)

def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc = ((crc << 8) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc

assert crc16(b"123456789") == 0x29B1, "CRC self-test failed"

# ---- frame build / parse ----------------------------------------------------
def build_frame(cw: int, payload: bytes = b"") -> bytes:
    pre = bytes([cw & 0xFF]) + (len(payload) + 2).to_bytes(2, "little") + payload
    return pre + crc16(pre).to_bytes(2, "little")

def connect_payload(epoch=None) -> bytes:
    epoch = int(time.time()) if epoch is None else epoch
    return bytes([0x02, 0x96, 0x00, 0x00]) + (epoch & 0xFFFFFFFF).to_bytes(4, "little")

def readlist_payload(ids) -> bytes:
    return b"".join(i.to_bytes(2, "little") for i in ids)

def write_payload(did: int, value: bytes, mode: str = "single") -> bytes:
    """0x14 dpWrite = [id][value]; 0x16 dpWriteList = [id][len u16][value] (one entry)."""
    if mode == "list":
        return did.to_bytes(2, "little") + len(value).to_bytes(2, "little") + value
    return did.to_bytes(2, "little") + value

# Friendly controls -> (dataPoint id, encoder). All u8. "power" is the inverse of ui_uistandby.
def _bool(s):
    s = s.lower()
    if s in ("1", "on", "true", "yes"): return 1
    if s in ("0", "off", "false", "no"): return 0
    raise ValueError(f"expected on/off, got {s!r}")
# Fan is set by LEVEL 1..6 on 0x138b (proven live: 2->3 moved 37%->52%, 1171->1538 rpm).
# 0x1388 (fan %) is read-only: writing it returns accessDenied. Manual level needs auto=off.
def _fan(s):
    n = int(s.lower().lstrip("l"))
    if not 1 <= n <= 6: raise ValueError("fan level must be 1..6 (or L1..L6)")
    return n
CONTROLS = {
    "power":    (0x139c, lambda s: 1 - _bool(s)),
    "standby":  (0x139c, _bool),
    "auto":     (0x139e, _bool),
    "light":    (0x139d, _bool),
    "lock":     (0x13a1, _bool),
    "identify": (0x13a2, _bool),
    "fan":      (0x138b, _fan),
}

def parse_frame(frame: bytes):
    """Parse one complete frame (len+3 bytes). Responses carry a status byte."""
    cw = frame[0]
    length = int.from_bytes(frame[1:3], "little")
    crc_ok = crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")
    is_resp = bool(cw & 0x80)
    if is_resp and length >= 3:
        status, payload = frame[3], frame[4:-2]
    else:
        status, payload = None, frame[3:-2]
    return {"cw": cw, "is_response": is_resp, "length": length, "status": status,
            "payload": payload, "crc_ok": crc_ok, "raw": frame}

def decode_tlv(p: bytes):
    """Best guess for read responses: repeat [id u16 LE][len u16 LE][value] (mirrors
    the writeList encoder). Returns list or None if it doesn't parse cleanly."""
    out, i = [], 0
    while i + 4 <= len(p):
        did = int.from_bytes(p[i:i+2], "little"); n = int.from_bytes(p[i+2:i+4], "little")
        if i + 4 + n > len(p):
            return None
        out.append((did, p[i+4:i+4+n])); i += 4 + n
    return out if i == len(p) else None

def _u(v): return int.from_bytes(v, "little")
def _s(v): return int.from_bytes(v, "little", signed=True)
def _epoch(v):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(_u(v)))

# Value decodings confirmed against the live unit (2026-10-21). Ints are little-endian.
# Counters tick in seconds (32 s wall clock -> +33 on tottimeonpower); air volume ~ +1 per
# ~28 s at CADR 130 m3/h, so m3. localtimesec = local wall clock as epoch (UTC-5 here).
DECODE = {
    0x0bcf: lambda v: f"{_u(v)} ug/m3", 0x0bd0: lambda v: f"{_u(v)} ug/m3",
    0x0bd1: lambda v: f"{_u(v)} ug/m3", 0x0bc5: lambda v: f"{_u(v)} rpm",
    0x0bd6: lambda v: f"{_u(v)} m3/h",
    0x1770: lambda v: f"{_u(v)} s ({_u(v)/3600:.1f} h)",
    0x1771: lambda v: f"{_u(v)} s ({_u(v)/3600:.1f} h)",
    0x1772: lambda v: f"{_u(v)} m3",
    0x0fc3: lambda v: f"UTC{_s(v)*15/60:+g}h (std)", 0x0fc5: lambda v: f"{_epoch(v)} UTC",
    0x9c49: lambda v: f"{_epoch(v)} local",
    0x100e: lambda v: f"{_s(v)} dBm",
    0x044c: lambda v: ".".join(map(str, v)), 0x044d: lambda v: ".".join(map(str, v)),
    0x044e: lambda v: ".".join(map(str, v)),
    0x1008: lambda v: ":".join(f"{b:02x}" for b in v), 0x753f: lambda v: ":".join(f"{b:02x}" for b in v),
    # u8 % remaining per filter slot; matched app 96/96/98 (pre & gas both 96 -> order of
    # the first two unproven; byte 2 = main particle/HyperHEPA; byte 3 = unused slot)
    0xc73e: lambda v: f"pre/gas {v[0]}% {v[1]}%  main {v[2]}%",
    0x13a5: lambda v: " ".join(f"{b}%" for b in v),
    **{i: (lambda v: "on" if _u(v) else "off")
       for i in (0x0fc4, 0x139c, 0x139d, 0x139e, 0x139f, 0x13a2, 0x9c4d)},
}

def fmt_value(did, v: bytes) -> str:
    if did in SECRET_IDS:
        return "<redacted>"
    if did in DECODE and v:
        return f"{DECODE[did](v):24s} [{v.hex()}]"
    s = v.hex()
    if 0 < len(v) <= 4:
        s += f"  (u={int.from_bytes(v,'little')}, s={int.from_bytes(v,'little',signed=True)})"
    if v and all(32 <= b < 127 for b in v.rstrip(b'\x00')):
        s += f"  '{v.rstrip(bytes(1)).decode()}'"
    return s

class Reassembler:
    """Accumulate notifications into whole frames (len+3 bytes each)."""
    def __init__(self): self.buf = b""
    def feed(self, data: bytes):
        self.buf += data
        frames = []
        while len(self.buf) >= 3:
            total = int.from_bytes(self.buf[1:3], "little") + 3
            if len(self.buf) < total:
                break
            frames.append(self.buf[:total]); self.buf = self.buf[total:]
        return frames

# ---- BLE plumbing -----------------------------------------------------------
_t0 = time.time()
def ts(): return f"+{time.time()-_t0:6.2f}s"

async def find():
    from bleak import BleakScanner
    print("scanning for IQAir ...", flush=True)
    dev = await BleakScanner.find_device_by_filter(
        # macOS shows the cached "IQAir ..." name; elsewhere the advert carries a coded
        # name, so also match IQAir's manufacturer id.
        lambda d, a: NAME_HINT.lower() in ((d.name or a.local_name or "").lower())
        or 0x060A in a.manufacturer_data, timeout=20)
    if not dev:
        sys.exit("IQAir not found (is it advertising / in range?)")
    print(f"found {dev.name} @ {dev.address}")
    return dev

async def run(args):
    from bleak import BleakClient
    dev = await find()
    async with BleakClient(dev, timeout=30.0) as c:
        try:
            v = await c.read_gatt_char(RX)
            print(f"{ts()}  bonded; RX read = {v.hex()}")
        except Exception as e:
            print(f"{ts()}  protected read failed ({e}); if a passkey dialog appears, enter the unit code")

        q: asyncio.Queue = asyncio.Queue()
        ra = Reassembler()
        def on_rx(_c, data: bytes):
            data = bytes(data)
            print(f"{ts()}  RX {len(data):3d}B {data.hex()}", flush=True)
            for fr in ra.feed(data):
                info = parse_frame(fr)
                st = info["status"]
                print(f"{ts()}  << frame cw=0x{info['cw']:02x} status="
                      f"{STATUS.get(st, st) if st is not None else '-'} crc_ok={info['crc_ok']} "
                      f"payload={info['payload'].hex()}", flush=True)
                q.put_nowait(info)
        await c.start_notify(RX, on_rx)
        print(f"{ts()}  subscribed RX")

        async def send(cw, payload=b""):
            frame = build_frame(cw, payload)
            print(f"{ts()}  >> TX cw=0x{cw:02x} {frame.hex()}")
            for i in range(0, len(frame), CHUNK):
                await c.write_gatt_char(TX, frame[i:i+CHUNK], response=True)

        async def await_resp(cw, timeout=5.0):
            """Collect frames for cw|0x80 until status != morePacketsFollow."""
            want, got = cw | 0x80, []
            end = time.time() + timeout
            while time.time() < end:
                try:
                    info = await asyncio.wait_for(q.get(), end - time.time())
                except asyncio.TimeoutError:
                    break
                if info["cw"] == want:
                    got.append(info)
                    if info["status"] != 0x80:
                        break
            return got

        if args.send is not None:
            # the device ignores everything until the 0x01 connect handshake
            await send(0x01, connect_payload())
            await await_resp(0x01)
            await send(args.send, bytes.fromhex(args.payload) if args.payload else b"")
            await asyncio.sleep(args.listen)
        elif args.read or args.dump or args.connect_only or args.set:
            await send(0x01, connect_payload())
            r = await await_resp(0x01)
            if not r:
                print(f"{ts()}  !! no 0x81 connectResponse — handshake not accepted")
            else:
                p = r[0]["payload"]
                if len(p) >= 3:
                    print(f"{ts()}  connected: protoVer={p[0]} rxBufSize={int.from_bytes(p[1:3],'little')}")
            if args.dump and r:
                # dpReadAll (0x15): device returns every dataPoint it has, incl. ids the app
                # has no name for, across several frames (status 0x80 = morePacketsFollow)
                await send(0x15)
                resp = await await_resp(0x15)
                tlv = decode_tlv(b"".join(x["payload"] for x in resp)) or []
                for did, val in tlv:
                    name = DATAPOINTS.get(did, f"unnamed_{did:04x}")
                    print(f"    0x{did:04x} {name:32s} {fmt_value(did, val)}")
                print(f"    ({len(tlv)} dataPoints)")
            elif args.set and r:
                async def read_ids(ids):
                    await send(0x13, readlist_payload(ids))
                    resp = await await_resp(0x13)
                    return dict(decode_tlv(b"".join(x["payload"] for x in resp)) or [])
                for item in args.set:
                    name, _, raw = item.partition("=")
                    if name in CONTROLS:
                        did, enc = CONTROLS[name]
                        val = bytes([enc(raw)])
                    else:   # raw form: 0xID=hexbytes
                        did, val = int(name, 16), bytes.fromhex(raw)
                    label = DATAPOINTS.get(did, f"0x{did:04x}")
                    before = (await read_ids([did])).get(did)
                    print(f"{ts()}  {label}: before={before.hex() if before else None} -> writing {val.hex()}")
                    cw = 0x16 if args.write_mode == "list" else 0x14
                    await send(cw, write_payload(did, val, args.write_mode))
                    wr = await await_resp(cw)
                    st = wr[-1]["status"] if wr else None
                    print(f"{ts()}  write status: {STATUS.get(st, st) if wr else 'NO RESPONSE'}")
                    await asyncio.sleep(1.0)
                    after = (await read_ids([did])).get(did)
                    ok = after == val
                    print(f"{ts()}  {label}: after={after.hex() if after else None}  "
                          f"{'CONFIRMED' if ok else 'NOT applied'}")
            elif args.read and r:
                ids = [i for i in DATAPOINTS if i not in SECRET_IDS] if args.ids == "all" else SENSOR_IDS
                # keep each request small; the device advertises its rx buffer size
                for k in range(0, len(ids), 8):
                    batch = ids[k:k+8]
                    await send(0x13, readlist_payload(batch))
                    resp = await await_resp(0x13)
                    if not resp:
                        print(f"{ts()}  !! no 0x93 for batch {[hex(i) for i in batch]}")
                        continue
                    body = b"".join(x["payload"] for x in resp)
                    tlv = decode_tlv(body)
                    if tlv is None:
                        print(f"{ts()}  (body didn't parse as [id][len][value] TLV; raw above)")
                        continue
                    for did, val in tlv:
                        print(f"    0x{did:04x} {DATAPOINTS.get(did, '?'):32s} {fmt_value(did, val)}")
                    missing = [i for i in batch if i not in {d for d, _ in tlv}]
                    if missing:
                        print("    (not returned: " + ", ".join(
                            f"0x{i:04x} {DATAPOINTS.get(i, '?')}" for i in missing) + ")")
            await asyncio.sleep(2)
        else:
            print(f"{ts()}  listening {args.listen:.0f}s ...")
            await asyncio.sleep(args.listen)

        await c.stop_notify(RX)
    print("done.")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", type=float, default=20.0, help="seconds to listen (passive / after --send)")
    ap.add_argument("--read", action="store_true", help="connect handshake, then dataPointReadList")
    ap.add_argument("--ids", default="sensors", choices=["sensors", "all"])
    ap.add_argument("--dump", action="store_true", help="connect, then dpReadAll (0x15) + decode everything")
    ap.add_argument("--connect-only", action="store_true", help="only the 0x01 -> 0x81 handshake")
    ap.add_argument("--send", type=lambda s: int(s, 16), help="raw controlWord (hex) to send once")
    ap.add_argument("--payload", help="payload hex for --send (default empty)")
    ap.add_argument("--set", nargs="+", metavar="NAME=VAL",
                    help="write + read back. names: " + ", ".join(CONTROLS) +
                         " (on/off; fan = level 1..6, needs auto=off), or raw 0xID=hex")
    ap.add_argument("--write-mode", choices=["single", "list"], default="single",
                    help="single = 0x14 dpWrite [id][val]; list = 0x16 dpWriteList [id][len][val]")
    asyncio.run(run(ap.parse_args()))

if __name__ == "__main__":
    main()
