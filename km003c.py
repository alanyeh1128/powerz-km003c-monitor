"""Driver for ChargerLAB POWER-Z KM003C over its vendor bulk interface (USB interface 0).

Protocol per https://github.com/okhsunrog/km003c-protocol-research:
- 4-byte LE header: type(7b) | reserved(1b) | tid(8b) | unused(1b) | attribute(15b)
- PutData (0x41) responses carry chained extended headers:
  attribute(15b) | next(1b) | chunk(6b) | size(10b)
"""
import binascii
import os
import struct
import time
from dataclasses import dataclass

import usb.core
import usb.util
from Crypto.Cipher import AES

VID, PID = 0x5FC9, 0x0063
EP_OUT, EP_IN = 0x01, 0x81
TIMEOUT_MS = 2000

CMD_CONNECT = 0x02
CMD_DISCONNECT = 0x03
CMD_ACCEPT = 0x05
CMD_GET_DATA = 0x0C
CMD_START_GRAPH = 0x0E
CMD_STOP_GRAPH = 0x0F
CMD_PUT_DATA = 0x41
CMD_MEMORY_READ = 0x44
CMD_STREAMING_AUTH = 0x4C

ATT_ADC = 0x0001
ATT_ADC_QUEUE = 0x0002
ATT_PD_PACKET = 0x0010

# Rate index -> samples per second for StartGraph / AdcQueue.
GRAPH_RATES = {0: 2, 1: 10, 2: 50, 3: 1000}

MEM_KEY = b"Lh2yfB7n6X7d9a5Z"
AUTH_KEY = b"Fa0b4tA25f4R038a"
ADDR_DEVICE_INFO = 0x00000420
ADDR_FIRMWARE_INFO = 0x00004420
ADDR_HARDWARE_ID = 0x40010450


class DeviceNotFound(RuntimeError):
    pass


@dataclass
class AdcSample:
    t: float            # host timestamp (epoch seconds)
    vbus: float         # V
    ibus: float         # A (signed; negative = female in -> male out, verified on FW 1.9.2)
    vbus_avg: float     # V, device-side averaged
    ibus_avg: float     # A, device-side averaged
    temp: float | None  # degC (None while graph streaming; device reports 0x8000)
    cc1: float          # V
    cc2: float          # V
    dp: float           # V (D+)
    dm: float           # V (D-)

    @property
    def power(self) -> float:
        return abs(self.vbus * self.ibus)


@dataclass
class QueueSample:
    seq: int            # device 1 kHz clock, wraps at 16 bits
    vbus: float
    ibus: float
    cc1: float
    cc2: float
    dp: float
    dm: float


def _cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", "replace")


class KM003C:
    def __init__(self):
        self.dev = None
        self._tid = 0
        self.authenticated = False
        self.info = {}

    # ---- low level ----
    def _send(self, cmd, word, payload=b""):
        self._tid = (self._tid + 1) & 0xFF
        self.dev.write(EP_OUT, bytes([cmd, self._tid]) + word + payload, timeout=TIMEOUT_MS)

    def _recv(self, timeout=TIMEOUT_MS) -> bytes:
        return bytes(self.dev.read(EP_IN, 4096, timeout=timeout))

    def _ctrl(self, cmd, attr=0, payload=b"") -> bytes:
        self._send(cmd, struct.pack("<H", attr << 1), payload)
        return self._recv()

    def _drain(self):
        try:
            while True:
                self.dev.read(EP_IN, 4096, timeout=30)
        except usb.core.USBError:
            pass

    def _get_data(self, attr):
        """GetData request; returns {attribute: (chunk, payload)} parsed from the PutData chain."""
        r = self._ctrl(CMD_GET_DATA, attr)
        if (r[0] & 0x7F) != CMD_PUT_DATA:
            raise RuntimeError(f"unexpected response: {r[:16].hex()}")
        out, off = {}, 4
        while off + 4 <= len(r):
            ext = struct.unpack_from("<I", r, off)[0]
            a, nxt, chunk, size = ext & 0x7FFF, (ext >> 15) & 1, (ext >> 16) & 0x3F, ext >> 22
            off += 4
            # AdcQueue reports per-sample size with chunk = sample count.
            n = size * chunk if a == ATT_ADC_QUEUE else size
            out[a] = (chunk, r[off:off + n])
            off += n
            if not nxt:
                break
        return out

    def _memory_read(self, addr, size) -> bytes:
        pt = struct.pack("<III", addr, size, 0xFFFFFFFF)
        pt += struct.pack("<I", binascii.crc32(pt)) + b"\xff" * 16
        self._send(CMD_MEMORY_READ, b"\x01\x01", AES.new(MEM_KEY, AES.MODE_ECB).encrypt(pt))
        buf = self._recv()
        if buf[0] != (CMD_MEMORY_READ | 0x80):
            raise RuntimeError(f"memory read rejected: {buf[:8].hex()}")
        need = (size + 15) // 16 * 16
        buf = buf[20:]
        while len(buf) < need:
            buf += self._recv()
        return AES.new(MEM_KEY, AES.MODE_ECB).decrypt(buf[:need])[:size]

    # ---- public API ----
    def open(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise DeviceNotFound("找不到 POWER-Z KM003C（請確認 USB 線已接上）")
        usb.util.claim_interface(dev, 0)
        self.dev = dev
        self._drain()
        r = self._ctrl(CMD_CONNECT)
        if r[0] != CMD_ACCEPT:
            raise RuntimeError(f"connect rejected: {r.hex()}")
        self.info = {"serial": usb.util.get_string(dev, dev.iSerialNumber) or ""}
        try:
            dinfo = self._memory_read(ADDR_DEVICE_INFO, 64)
            finfo = self._memory_read(ADDR_FIRMWARE_INFO, 64)
            self.info.update(model=_cstr(dinfo[16:28]), hw_version=_cstr(dinfo[28:40]),
                             hw_date=_cstr(dinfo[40:56]), fw_version=_cstr(finfo[28:40]),
                             fw_date=_cstr(finfo[40:56]))
        except Exception as e:
            self.info["info_error"] = str(e)
        self.authenticated = False
        try:
            self._authenticate()
        except Exception as e:
            self.info["auth_error"] = str(e)
        self.info["authenticated"] = self.authenticated

    def _authenticate(self):
        hwid = self._memory_read(ADDR_HARDWARE_ID, 12)
        pt = struct.pack("<Q", int(time.time() * 1000)) + hwid + os.urandom(12)
        r = self._ctrl_raw(CMD_STREAMING_AUTH, b"\x00\x02", AES.new(AUTH_KEY, AES.MODE_ECB).encrypt(pt))
        attr = struct.unpack_from("<H", r, 2)[0]
        self.authenticated = r[0] == CMD_STREAMING_AUTH and ((attr >> 1) & 3) >= 1

    def _ctrl_raw(self, cmd, word, payload=b"") -> bytes:
        self._send(cmd, word, payload)
        return self._recv()

    def close(self):
        if self.dev is None:
            return
        try:
            try:
                self._ctrl(CMD_STOP_GRAPH)
                self._ctrl(CMD_DISCONNECT)
            except Exception:
                pass
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        finally:
            self.dev = None

    def read_adc(self) -> AdcSample:
        _, p = self._get_data(ATT_ADC)[ATT_ADC]
        vbus, ibus, vbus_avg, ibus_avg = struct.unpack_from("<iiii", p, 0)
        temp_raw, cc1, cc2, dp, dm = struct.unpack_from("<hHHHH", p, 24)
        return AdcSample(
            t=time.time(),
            vbus=vbus / 1e6, ibus=ibus / 1e6,
            vbus_avg=vbus_avg / 1e6, ibus_avg=ibus_avg / 1e6,
            temp=None if temp_raw == -0x8000 else temp_raw / 128.0,
            cc1=cc1 / 1e4, cc2=cc2 / 1e4, dp=dp / 1e4, dm=dm / 1e4,
        )

    def start_graph(self, rate_index):
        r = self._ctrl(CMD_START_GRAPH, rate_index)
        if r[0] != CMD_ACCEPT:
            raise RuntimeError(f"start graph rejected: {r.hex()}")

    def stop_graph(self):
        self._ctrl(CMD_STOP_GRAPH)
        self._drain()

    def read_queue(self, rate_index) -> list[QueueSample]:
        chunk, p = self._get_data(ATT_ADC_QUEUE).get(ATT_ADC_QUEUE, (0, b""))
        aux = 1e-4 if rate_index == 0 else 1e-3
        out = []
        for k in range(min(chunk, len(p) // 20)):
            seq, _marker, v, i, cc1, cc2, dp, dm = struct.unpack_from("<HHiiHHHH", p, k * 20)
            out.append(QueueSample(seq, v / 1e6, i / 1e6, cc1 * aux, cc2 * aux, dp * aux, dm * aux))
        return out

    def read_pd(self):
        """Returns (preamble dict | None, list of events). Events are dicts with kind 'conn' or 'msg'."""
        _, p = self._get_data(ATT_PD_PACKET).get(ATT_PD_PACKET, (0, b""))
        if len(p) < 12:
            return None, []
        ts, vbus, ibus, cc1, cc2 = struct.unpack_from("<IHhHH", p, 0)
        pre = {"ts_ms": ts, "vbus": vbus / 1e3, "ibus": ibus / 1e3, "cc1": cc1 / 1e3, "cc2": cc2 / 1e3}
        events, i = [], 12
        while i < len(p):
            b = p[i]
            if b == 0x45 and i + 6 <= len(p):
                ts24 = p[i + 1] | p[i + 2] << 8 | p[i + 3] << 16
                code = p[i + 5]
                events.append({"kind": "conn", "ts_ms": ts24,
                               "event": {0x21: "Attach", 0x22: "Detach"}.get(code, f"Event 0x{code:02X}")})
                i += 6
            elif 0x80 <= b <= 0x9F and i + 6 <= len(p):
                ts32 = struct.unpack_from("<I", p, i + 1)[0]
                sop = p[i + 5]
                wire_len = (b & 0x3F) - 5
                wire = p[i + 6:i + 6 + wire_len]
                events.append({"kind": "msg", "ts_ms": ts32, "sop": sop, "wire": wire})
                i += 6 + wire_len
            else:
                break
        return pre, events
