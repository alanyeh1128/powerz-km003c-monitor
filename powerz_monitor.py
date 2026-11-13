#!/usr/bin/env python3
"""POWER-Z KM003C monitor: streams high-rate ADC data and PD packets to a web dashboard.

Usage: .venv/bin/python powerz_monitor.py [--port 8780] [--rate 2] [--no-browser]
  --rate: 0=2 SPS, 1=10 SPS, 2=50 SPS, 3=1000 SPS
"""
import argparse
import collections
import csv
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from km003c import GRAPH_RATES, KM003C, DeviceNotFound
from pd_decode import PdDecoder

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ROOT, "logs")
SUMMARY_FIELDS = ["file", "started", "rate_sps", "samples", "duration_s", "avg_V", "avg_A",
                  "avg_W", "min_W", "max_W", "energy_mWh", "charge_mAh", "pd_messages"]
DISK_CHECK_S = 2.0             # free-space check period while running
CSV_FIELDS = ["timestamp", "elapsed_s", "vbus_V", "ibus_A", "power_W", "dp_V", "dm_V", "cc1_V", "cc2_V"]
HISTORY_LEN = 300_000          # samples kept server-side for page reloads
POLL_S = 0.04                  # AdcQueue / PD poll period
METER_S = 0.25                 # ADC snapshot period (temperature, averages)
MAX_GAP_S = 2.0                # longer gaps are not integrated into energy


class Stats:
    """Running statistics with trapezoidal energy (Wh) and charge (Ah) integration."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.start = self.last_t = self.last_p = self.last_i = None
        self.n = 0
        self.sum_p = self.sum_i = self.sum_v = 0.0
        self.min_p = self.max_p = None
        self.energy_j = self.charge_c = 0.0

    def add(self, t, v, i, p):
        i = abs(i)
        if self.start is None:
            self.start = t
        if self.last_t is not None and 0 < t - self.last_t <= MAX_GAP_S:
            dt = t - self.last_t
            self.energy_j += (p + self.last_p) / 2 * dt
            self.charge_c += (i + self.last_i) / 2 * dt
        self.last_t, self.last_p, self.last_i = t, p, i
        self.n += 1
        self.sum_p += p
        self.sum_i += i
        self.sum_v += v
        self.min_p = p if self.min_p is None else min(self.min_p, p)
        self.max_p = p if self.max_p is None else max(self.max_p, p)

    def to_dict(self):
        n = self.n or 1
        return {
            "samples": self.n,
            "duration_s": (self.last_t - self.start) if self.n > 1 else 0.0,
            "avg_V": self.sum_v / n, "avg_A": self.sum_i / n, "avg_W": self.sum_p / n,
            "min_W": self.min_p or 0.0, "max_W": self.max_p or 0.0,
            "energy_Wh": self.energy_j / 3600, "charge_Ah": self.charge_c / 3600,
        }


class Monitor:
    def __init__(self, rate_index, min_free_bytes):
        self.meter = KM003C()
        self.min_free = min_free_bytes
        self.disk_free = None
        self._next_disk = 0.0
        self.rate_index = rate_index
        self.lock = threading.Lock()
        self.cmds = queue.Queue()
        self.listeners = set()
        self.connected = False
        self.streaming = False
        self.error = ""
        self.error_code, self.error_params = None, {}
        self.session = Stats()
        self.history = collections.deque(maxlen=HISTORY_LEN)
        self.last_adc = None
        self.last_temp = None
        self.dropped = 0
        self.pd = PdDecoder()
        self.pd_log = []
        self.pd_t0 = None
        self._seq_anchor = None
        self.offline = False  # offline review: the meter is released and not polled
        self.paused = False   # sampling stopped by the user; the meter stays connected but idle
        self._halted = False  # the worker has actually stopped the stream for self.paused
        # Recording state
        self.rec_file = self.rec_writer = self.rec_name = self.rec_stats = None
        self.records = self._load_records()

    # ---------------- device loop ----------------
    def run(self):
        while True:
            if self.offline:
                if self.connected:
                    self._release()
                time.sleep(0.2)
                continue
            if not self.connected:
                if not self._connect():
                    time.sleep(1)
                    continue
            try:
                self._handle_commands()
                if self.paused:
                    if not self._halted:
                        self.meter.stop_graph()
                        self.streaming, self._halted = False, True
                        self._broadcast({"type": "status", **self.status()})
                    time.sleep(0.1)
                    continue
                if self._halted:
                    self._halted = False
                    self._start_stream()
                    self._next_meter = self._next_pd = 0.0
                    self._broadcast({"type": "status", **self.status()})
                self._poll()
            except Exception as e:
                self._disconnect("usb", f"USB 通訊錯誤：{e}", detail=str(e))
                time.sleep(1)

    def _connect(self):
        try:
            self.meter.open()
        except Exception as e:
            self.meter.close()
            if isinstance(e, DeviceNotFound):
                self._set_error("device_not_found", str(e))
            else:
                self._set_error("usb", f"USB 通訊錯誤：{e}", detail=str(e))
            self._broadcast({"type": "status", **self.status()})
            return False
        self.connected = True
        self._halted = False
        self._set_error(None, "")
        self.meter.stop_graph()
        t = self.meter.read_adc().temp
        if t is not None:
            self.last_temp = t
        self._start_stream()
        self._next_meter = self._next_pd = 0.0
        self._broadcast({"type": "status", **self.status()})
        return True

    def _set_error(self, code, msg, **params):
        """Errors carry a code + params so the web UI can show them in its own language; msg is a zh-Hant fallback."""
        self.error, self.error_code, self.error_params = msg, code, params

    def _disconnect(self, code, msg, **params):
        self.meter.close()
        self.connected = self.streaming = False
        self._set_error(code, msg, **params)
        self._broadcast({"type": "status", **self.status()})

    def _release(self):
        """Stop streaming and hand the USB interface back (entering offline review)."""
        self.meter.close()
        self.connected = self.streaming = False
        self._set_error(None, "")
        self._broadcast({"type": "status", **self.status()})

    def _start_stream(self):
        self._seq_anchor = None
        self.streaming = False
        if self.meter.authenticated:
            self.meter.start_graph(self.rate_index)
            self.streaming = True

    def _handle_commands(self):
        while True:
            try:
                cmd, arg = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd == "rate" and arg != self.rate_index:
                if self._halted:  # stopped: just remember it for the next start
                    self.rate_index = arg
                    self._broadcast({"type": "status", **self.status()})
                    continue
                if self.streaming:
                    self.meter.stop_graph()
                self.rate_index = arg
                self._start_stream()
                self._broadcast({"type": "status", **self.status()})

    def _poll(self):
        t_loop = time.monotonic()
        now = time.time()
        samples = []
        if self.streaming:
            qs = self.meter.read_queue(self.rate_index)
            for q, t in zip(qs, self._seq_to_times([q.seq for q in qs], now)):
                samples.append((t, q.vbus, q.ibus, q.dp, q.dm, q.cc1, q.cc2))
        if now >= self._next_meter or not self.streaming:
            a = self.meter.read_adc()
            if a.temp is not None:
                self.last_temp = a.temp
            self.last_adc = a
            self._next_meter = now + METER_S
            if not self.streaming:  # fallback without auth: ADC snapshots at ~10 Hz
                samples.append((a.t, a.vbus, a.ibus, a.dp, a.dm, a.cc1, a.cc2))
        if now >= self._next_pd:
            self._next_pd = now + 0.1
            self._poll_pd(now)
        if samples:
            self._ingest(samples)
        if self.last_adc and now >= getattr(self, "_next_meter_msg", 0):
            self._next_meter_msg = now + METER_S
            self._broadcast_meter()
        period = POLL_S if self.streaming else 0.1
        time.sleep(max(0.0, period - (time.monotonic() - t_loop)))

    def _seq_to_times(self, seqs, now):
        """Map the device's 16-bit 1 kHz sample clock onto host epoch time."""
        if not seqs:
            return []
        a = self._seq_anchor
        expected = 1000 // GRAPH_RATES[self.rate_index]
        offsets, u = [], 0
        last = seqs[0] if a is None else a["last"]
        for seq in seqs:
            step = (seq - last) & 0xFFFF
            if step > expected * 1.5:
                self.dropped += round(step / expected) - 1
            u += step
            offsets.append(u)
            last = seq
        if a is None:
            # The newest sample of the batch is "now"; earlier backlog is placed before it.
            a = self._seq_anchor = {"host": now - offsets[-1] / 1000.0, "unwrapped": 0}
        a["last"] = last
        times = [a["host"] + (a["unwrapped"] + o) / 1000.0 for o in offsets]
        a["unwrapped"] += offsets[-1]
        # Re-anchor on clock drift / long stalls so timestamps track host time.
        if abs(times[-1] - now) > 1.0:
            shift = now - times[-1]
            a["host"] += shift
            times = [t + shift for t in times]
        return times

    def _ingest(self, samples):
        cols = {k: [] for k in ("t", "v", "i", "p", "dp", "dm", "cc1", "cc2")}
        with self.lock:
            for t, v, i, dp, dm, cc1, cc2 in samples:
                p = abs(v * i)
                self.session.add(t, v, i, p)
                self.history.append((t, v, i, p, dp, dm, cc1, cc2))
                for k, x in zip(cols, (t, v, i, p, dp, dm, cc1, cc2)):
                    cols[k].append(round(x, 3) if k == "t" else round(x, 6))
                if self.rec_writer:
                    self.rec_stats.add(t, v, i, p)
                    self.rec_writer.writerow([
                        datetime.fromtimestamp(t).isoformat(timespec="milliseconds"),
                        f"{t - self.rec_stats.start:.3f}", f"{v:.6f}", f"{i:.6f}", f"{p:.6f}",
                        f"{dp:.4f}", f"{dm:.4f}", f"{cc1:.4f}", f"{cc2:.4f}"])
        self._broadcast({"type": "samples", **cols})

    def _check_disk(self):
        """Refresh free space; stop recording before the disk fills up."""
        now = time.monotonic()
        if now < self._next_disk:
            return
        self._next_disk = now + DISK_CHECK_S
        os.makedirs(LOG_DIR, exist_ok=True)
        self.disk_free = shutil.disk_usage(LOG_DIR).free
        if self.rec_writer and self.disk_free < self.min_free:
            r = self.stop_record()
            if r["ok"]:
                self._broadcast({"type": "alert", "record_stopped": True, "code": "disk_low_stopped",
                                 "params": {"free": fmt_bytes(self.disk_free), "min": fmt_bytes(self.min_free),
                                            "name": r["summary"]["name"]}, "message":
                                 f"硬碟剩餘空間只剩 {fmt_bytes(self.disk_free)}（低於 {fmt_bytes(self.min_free)}），"
                                 f"已自動停止錄製並保存「{r['summary']['name']}」。"})

    def _broadcast_meter(self):
        a = self.last_adc
        self._check_disk()
        with self.lock:
            rec = None
            if self.rec_writer:
                # Flush every meter tick (~4 Hz): a crash loses at most ~0.25 s, and the reported size is real.
                self.rec_file.flush()
                rec = {"name": self.rec_name, "bytes": os.fstat(self.rec_file.fileno()).st_size,
                       **self.rec_stats.to_dict()}
            msg = {"type": "meter", "v": a.vbus, "i": a.ibus, "p": a.power,
                   "v_avg": a.vbus_avg, "i_avg": a.ibus_avg, "p_avg": abs(a.vbus_avg * a.ibus_avg),
                   "temp": self.last_temp, "dp": a.dp, "dm": a.dm, "cc1": a.cc1, "cc2": a.cc2,
                   "session": self.session.to_dict(), "recording": rec, "dropped": self.dropped,
                   "disk_free": self.disk_free, "min_free": self.min_free}
        self._broadcast(msg)

    def _poll_pd(self, now):
        pre, events = self.meter.read_pd()
        if not events:
            return
        out = []
        for ev in events:
            if self.pd_t0 is None:
                self.pd_t0 = ev["ts_ms"]
            item = {"n": len(self.pd_log) + len(out) + 1, "host_t": now,
                    "dev_s": (ev["ts_ms"] - self.pd_t0) / 1000.0,
                    "vbus": pre["vbus"] if pre else None, "ibus": pre["ibus"] if pre else None}
            if ev["kind"] == "conn":
                if ev["event"] == "Attach":
                    self.pd.reset()
                item.update(kind="conn", name=ev["event"], summary="", sop="", roles="", details=[], raw="")
            else:
                item.update(kind="msg", **self.pd.decode(ev["wire"], ev["sop"]))
            out.append(item)
        with self.lock:
            self.pd_log.extend(out)
            if self.rec_writer:
                self._rec_pd.extend(out)
        self._broadcast({"type": "pd", "events": out})

    def _broadcast(self, msg):
        data = json.dumps(msg)
        for q in list(self.listeners):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    # ---------------- control API ----------------
    def status(self):
        return {"connected": self.connected, "error": self.error, "error_code": self.error_code,
                "error_params": self.error_params, "info": self.meter.info,
                "streaming": self.streaming, "rate_index": self.rate_index,
                "rates": GRAPH_RATES, "offline": self.offline, "paused": self.paused}

    def set_rate(self, idx):
        if idx not in GRAPH_RATES:
            return fail("invalid_rate", "取樣率不正確")
        self.cmds.put(("rate", idx))
        return {"ok": True}

    def set_offline(self, offline):
        with self.lock:
            if offline and self.rec_writer:
                return fail("recording_offline", "錄製中無法切換到離線模式，請先停止錄製。")
            self.offline = offline
        # The worker releases (or reconnects) the meter and broadcasts again once that's done.
        self._broadcast({"type": "status", **self.status()})
        return {"ok": True}

    def set_paused(self, paused):
        with self.lock:
            if paused and self.rec_writer:
                return fail("recording_pause", "錄製中無法停止取樣，請先停止錄製。")
            resume = self.paused and not paused
        if resume:
            self.reset_session()  # Start begins a fresh run: clear before the worker samples again
        with self.lock:
            self.paused = paused
        # The worker stops / restarts the stream and broadcasts again once that's done.
        self._broadcast({"type": "status", **self.status()})
        return {"ok": True}

    def start_record(self, label):
        with self.lock:
            if self.paused:
                return fail("paused_no_record", "已停止取樣，無法錄製。請先按「開始取樣」。")
            if self.offline:
                return fail("offline_no_record", "離線模式無法錄製")
            if self.rec_writer:
                return fail("recording", "已在錄製中")
            os.makedirs(LOG_DIR, exist_ok=True)
            free = shutil.disk_usage(LOG_DIR).free
            if free < self.min_free:
                return fail("disk_low_start", f"硬碟剩餘空間只剩 {fmt_bytes(free)}，"
                                              f"低於保護門檻 {fmt_bytes(self.min_free)}，無法開始錄製。",
                            free=fmt_bytes(free), min=fmt_bytes(self.min_free))
            label = re.sub(r"[^\w\-]+", "_", label.strip())[:60] or "run"
            name = f"{datetime.now():%Y%m%d_%H%M%S}_{label}.csv"
            self.rec_file = open(os.path.join(LOG_DIR, name), "w", newline="")
            self.rec_writer = csv.writer(self.rec_file)
            self.rec_writer.writerow(CSV_FIELDS)
            self.rec_name, self.rec_stats, self._rec_pd = name, Stats(), []
            self.rec_started = time.time()
            return {"ok": True, "name": name}

    def stop_record(self):
        with self.lock:
            if not self.rec_writer:
                return fail("not_recording", "目前沒有在錄製")
            self.rec_file.close()
            s = self.rec_stats.to_dict()
            summary = {"name": self.rec_name, "started": self.rec_started,
                       "rate_sps": GRAPH_RATES[self.rate_index] if self.streaming else 10,
                       "pd_messages": len(self._rec_pd), **s}
            if self._rec_pd:
                with open(os.path.join(LOG_DIR, self.rec_name[:-4] + "_pd.csv"), "w", newline="") as f:
                    f.write(pd_csv(self._rec_pd))
            self.records.insert(0, summary)
            self._write_summary()
            self.rec_file = self.rec_writer = self.rec_name = self.rec_stats = None
            return {"ok": True, "summary": summary}

    def _load_records(self):
        path = os.path.join(LOG_DIR, "summary.csv")
        out = []
        try:
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    if not os.path.exists(os.path.join(LOG_DIR, r.get("file") or "")):
                        continue
                    try:
                        out.insert(0, {
                            "name": r["file"], "started": datetime.fromisoformat(r["started"]).timestamp(),
                            "rate_sps": float(r["rate_sps"]), "samples": int(r["samples"]),
                            "duration_s": float(r["duration_s"]), "avg_V": float(r["avg_V"]),
                            "avg_A": float(r["avg_A"]), "avg_W": float(r["avg_W"]),
                            "min_W": float(r["min_W"]), "max_W": float(r["max_W"]),
                            "energy_Wh": float(r["energy_mWh"]) / 1000, "charge_Ah": float(r["charge_mAh"]) / 1000,
                            "pd_messages": int(r["pd_messages"])})
                    except (KeyError, ValueError):
                        continue
        except FileNotFoundError:
            pass
        return out

    def _write_summary(self):
        """Rewrite logs/summary.csv from self.records (caller holds the lock)."""
        path = os.path.join(LOG_DIR, "summary.csv")
        with open(path + ".tmp", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(SUMMARY_FIELDS)
            for r in reversed(self.records):
                w.writerow([r["name"], datetime.fromtimestamp(r["started"]).isoformat(timespec="seconds"),
                            r["rate_sps"], r["samples"], f"{r['duration_s']:.2f}", f"{r['avg_V']:.4f}",
                            f"{r['avg_A']:.5f}", f"{r['avg_W']:.4f}", f"{r['min_W']:.4f}", f"{r['max_W']:.4f}",
                            f"{r['energy_Wh'] * 1000:.3f}", f"{r['charge_Ah'] * 1000:.3f}", r["pd_messages"]])
        os.replace(path + ".tmp", path)

    def _find_record(self, name):
        return next((r for r in self.records if r["name"] == name), None)

    def rename_record(self, name, new_stem):
        with self.lock:
            rec = self._find_record(name)
            if rec is None:
                return fail("record_not_found", "找不到這筆紀錄")
            stem = re.sub(r"[^\w\-]+", "_", new_stem.strip()).strip("_")[:100]
            if not stem:
                return fail("name_empty", "檔名不可空白")
            if stem.endswith("_pd") or stem == "summary":
                return fail("name_reserved", "這個檔名保留給系統使用，請換一個")
            new = stem + ".csv"
            if new == name:
                return {"ok": True, "name": name}
            old_pd, new_pd = name[:-4] + "_pd.csv", stem + "_pd.csv"
            # A case-only rename hits the same file on case-insensitive filesystems (APFS default); not a collision.
            if new.lower() != name.lower() and any(os.path.exists(os.path.join(LOG_DIR, n)) for n in (new, new_pd)):
                return fail("name_exists", f"已經有名為「{new}」的檔案", name=new)
            os.rename(os.path.join(LOG_DIR, name), os.path.join(LOG_DIR, new))
            if os.path.exists(os.path.join(LOG_DIR, old_pd)):
                os.rename(os.path.join(LOG_DIR, old_pd), os.path.join(LOG_DIR, new_pd))
            rec["name"] = new
            self._write_summary()
            return {"ok": True, "name": new}

    def delete_record(self, name):
        with self.lock:
            rec = self._find_record(name)
            if rec is None:
                return fail("record_not_found", "找不到這筆紀錄")
            for n in (name, name[:-4] + "_pd.csv"):
                try:
                    os.remove(os.path.join(LOG_DIR, n))
                except FileNotFoundError:
                    pass
            self.records.remove(rec)
            self._write_summary()
            return {"ok": True}

    def reset_session(self):
        """Start over: totals, chart history and the PD log. A recording in progress keeps going untouched."""
        with self.lock:
            self.session.reset()
            self.dropped = 0
            self.history.clear()
            self.pd_log = []
            self.pd_t0 = None
        self._broadcast({"type": "reset"})
        return {"ok": True}

    def clear_pd(self):
        with self.lock:
            self.pd_log = []
            self.pd_t0 = None
        return {"ok": True}

    def state(self):
        with self.lock:
            records = []
            for r in self.records:
                path = os.path.join(LOG_DIR, r["name"])
                pd_name = r["name"][:-4] + "_pd.csv"
                records.append({**r, "size": os.path.getsize(path) if os.path.exists(path) else 0,
                                "pd_file": pd_name if os.path.exists(os.path.join(LOG_DIR, pd_name)) else None})
            return {**self.status(), "recording": self.rec_name, "records": records, "pd": self.pd_log,
                    "disk_free": shutil.disk_usage(LOG_DIR).free if os.path.isdir(LOG_DIR) else None,
                    "min_free": self.min_free}

    def history_json(self):
        with self.lock:
            rows = list(self.history)
        cols = list(zip(*rows)) if rows else [[]] * 8
        keys = ("t", "v", "i", "p", "dp", "dm", "cc1", "cc2")
        return {k: [round(x, 3) if k == "t" else round(x, 6) for x in c] for k, c in zip(keys, cols)}


def fail(code, msg, **params):
    """API error: code + params for the localized UI, msg as a zh-Hant fallback."""
    return {"ok": False, "code": code, "params": params, "error": msg}


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def pd_csv(events):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["no", "host_time", "device_s", "sop", "roles", "message", "msg_id", "summary", "vbus_V", "ibus_A", "raw"])
    for e in events:
        w.writerow([e["n"], datetime.fromtimestamp(e["host_t"]).isoformat(timespec="milliseconds"),
                    f"{e['dev_s']:.3f}", e.get("sop", ""), e.get("roles", ""), e["name"], e.get("msg_id", ""),
                    e.get("summary", ""), e.get("vbus", ""), e.get("ibus", ""), e.get("raw", "")])
    return buf.getvalue()


def make_handler(mon):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def log_message(self, *a):
            pass

        def _send(self, body, ctype, extra=None):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self.path = "/static/index.html"
                return super().do_GET()
            if path == "/api/state":
                return self._json(mon.state())
            if path == "/api/history":
                return self._json(mon.history_json())
            if path == "/api/pd.csv":
                with mon.lock:
                    body = pd_csv(mon.pd_log).encode("utf-8-sig")
                return self._send(body, "text/csv", {
                    "Content-Disposition": f'attachment; filename="pd_{datetime.now():%Y%m%d_%H%M%S}.csv"'})
            if path == "/events":
                return self._events()
            if path.startswith("/static/") or path.startswith("/logs/"):
                if ".." in unquote(path):
                    return self.send_error(403)
                return super().do_GET()
            self.send_error(404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            routes = {
                "/api/record/start": lambda: mon.start_record(body.get("label", "")),
                "/api/record/stop": mon.stop_record,
                "/api/session/reset": mon.reset_session,
                "/api/pd/clear": mon.clear_pd,
                "/api/sampling": lambda: mon.set_paused(not body.get("run", True)),
                "/api/mode": lambda: mon.set_offline(bool(body.get("offline"))),
                "/api/rate": lambda: mon.set_rate(int(body.get("rate_index", -1))),
                "/api/records/rename": lambda: mon.rename_record(str(body.get("name", "")), str(body.get("new_name", ""))),
                "/api/records/delete": lambda: mon.delete_record(str(body.get("name", ""))),
            }
            if self.path not in routes:
                return self.send_error(404)
            self._json(routes[self.path]())

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue(maxsize=2000)
            mon.listeners.add(q)
            try:
                self.wfile.write(f"data: {json.dumps({'type': 'status', **mon.status()})}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        data = q.get(timeout=5)
                    except queue.Empty:
                        data = '{"type":"ping"}'
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                mon.listeners.discard(q)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--rate", type=int, default=2, choices=sorted(GRAPH_RATES), help="sample rate index")
    ap.add_argument("--min-free-gb", type=float, default=1.0,
                    help="stop recording when free disk space drops below this (GB)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    mon = Monitor(args.rate, int(args.min_free_gb * 1024 ** 3))
    threading.Thread(target=mon.run, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(mon))
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"POWER-Z monitor running at {url}  (Ctrl+C to quit)")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if mon.rec_writer:
            mon.stop_record()
        mon.meter.close()


if __name__ == "__main__":
    main()
