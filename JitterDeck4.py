#!/usr/bin/env python3
"""
JitterDeck 4 — cleaned RF workbench (CC1101 + NRF1/2/3).
File kept as JitterDeck3.py so existing launchers / shortcuts still work.

What's new vs. JitterDeck 3:
  * Dead Noop meter code removed
  * Duplicate firmware modes collapsed to one (ESP32 native)
  * Broken quick-command buttons fixed (correct byte counts per firmware)
  * RSSI gate removed (firmware doesn't emit RSSI in this build)
  * Capture bank now persists to ~/.jitterdeck/captures.json
  * Sub-GHz frequency sweep (RX only): 315 / 390 / 433.92 / 868.35 / 915 MHz
  * NRF 2.4 GHz occupancy monitor (RX only): 0-125 channel heatmap
  * Layout reshuffled: INGEST / ANALYZE / EXPERIMENTAL / BANK / CONSOLE
  * Reserved EXPERIMENTAL panel for future legal features

No transmit-side jamming, flooding, or interference features are included.
All analysis is receive-only.
"""

import os
import re
import glob
import time
import json
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import serial

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRESET_MAP = {
    "AM650 (OOK 650kHz)": "FuriHalSubGhzPresetOok650Async",
    "AM270 (OOK 270kHz)": "FuriHalSubGhzPresetOok270Async",
    "FM238 (2-FSK 2.38k)": "FuriHalSubGhzPreset2FSKDev238Async",
    "FM476 (2-FSK 47.6k)": "FuriHalSubGhzPreset2FSKDev476Async",
    "NRF24 2Mbps (2.4G)": "NRF24_2MBPS",
    "NRF24 1Mbps (2.4G)": "NRF24_1MBPS",
    "NRF24 250kbps (2.4G)": "NRF24_250KBPS",
}
REV_PRESET_MAP = {v: k for k, v in PRESET_MAP.items()}

COMMON_FREQS = ["315.00", "390.00", "415.00", "433.92", "868.35", "915.00",
                "2402.00", "2426.00", "2450.00", "2476.00", "2480.00"]

SWEEP_FREQS_MHZ = [315.00, 390.00, 433.92, 868.35, 915.00]

RADIO_SPECS = {
    "CC1101": {"band": "Sub-GHz (300-928 MHz)", "pins": "CS 5 / GDO0 IRQ"},
    "NRF1":   {"band": "2.4 GHz (CH 0-125)",    "pins": "CSN 4 / CE 15"},
    "NRF2":   {"band": "2.4 GHz (CH 0-125)",    "pins": "CSN 48 / CE 47"},
    "NRF3":   {"band": "2.4 GHz (CH 0-125)",    "pins": "CSN 21 / CE 14"},
}

PERSIST_DIR = os.path.expanduser("~/.jitterdeck")
BANK_PATH = os.path.join(PERSIST_DIR, "captures.json")


def ensure_persist():
    os.makedirs(PERSIST_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Rolling-code decoder (preserved unchanged — this is genuinely good code)
# ---------------------------------------------------------------------------

class RollingCodeDecoder:
    """Multi-vendor Sub-GHz rolling-code demodulator & classifier (PWM / Manchester)."""

    @staticmethod
    def demodulate_pulses(pulses):
        clean = [abs(p) for p in pulses if 120 <= abs(p) <= 4500]
        if len(clean) < 24:
            return []
        sorted_w = sorted(clean)
        short_ref = sorted_w[len(sorted_w) // 4]
        long_ref = sorted_w[(len(sorted_w) * 3) // 4]
        mid = (short_ref + long_ref) / 2.0
        results = []

        pwm_bits = []
        for i in range(0, len(pulses) - 1, 2):
            hi, lo = abs(pulses[i]), abs(pulses[i + 1])
            if 120 <= hi <= 4500 and 120 <= lo <= 4500:
                pwm_bits.append("1" if hi > mid else "0")
        if len(pwm_bits) >= 24:
            results.append(("PWM", "".join(pwm_bits)))

        man_bits, half_t, phase = [], short_ref, 0
        for p in pulses:
            dur = abs(p)
            if dur < 100 or dur > 4500:
                continue
            steps = max(1, min(2, round(dur / max(half_t, 120))))
            level = "1" if p > 0 else "0"
            for _ in range(steps):
                phase += 1
                if phase % 2 == 0:
                    man_bits.append(level)
        if len(man_bits) >= 24:
            results.append(("MANCHESTER", "".join(man_bits)))
        return results

    @classmethod
    def analyze(cls, pulses, freq_hz=315000000, preset=""):
        candidates = cls.demodulate_pulses(pulses)
        if not candidates:
            return None
        best_enc, best_bits = max(candidates, key=lambda x: len(x[1]))
        trimmed = re.sub(r"^(?:01){4,}|^(?:10){4,}", "", best_bits)
        if len(trimmed) < 24:
            trimmed = best_bits
        bl = len(trimmed)
        is_fsk = "2FSK" in preset or "FM" in preset
        mhz = round(freq_hz / 1e6, 2)
        proto, serial_bits, hop_bits = "ROLLING-RAW", "", ""

        if 64 <= bl <= 68 and best_enc == "PWM" and not is_fsk:
            proto, hop_bits, serial_bits = "KeeLoq-66", trimmed[:32], trimmed[32:64]
        elif 50 <= bl <= 54 and best_enc == "PWM" and not is_fsk:
            proto, hop_bits, serial_bits = "Nice-FlorS", trimmed[:28], trimmed[28:52]
        elif 60 <= bl <= 66 and is_fsk:
            proto = "Subaru/Denso-64" if abs(mhz - 315.0) < 1.0 else "Auto-FSK-64"
            serial_bits, hop_bits = trimmed[:32], trimmed[32:64]
        elif 70 <= bl <= 84 and is_fsk:
            proto, serial_bits, hop_bits = "Ford/Honda-FSK", trimmed[:36], trimmed[36:72]
        elif 92 <= bl <= 108 and is_fsk:
            proto, serial_bits, hop_bits = "VAG/BMW-FSK", trimmed[:48], trimmed[48:96]
        elif 38 <= bl <= 44 and best_enc == "MANCHESTER":
            proto, serial_bits, hop_bits = "Security+2.0", trimmed[:20], trimmed[20:40]
        elif 56 <= bl <= 72 and best_enc == "MANCHESTER":
            proto, serial_bits, hop_bits = "StarLine/Sherkhan", trimmed[:32], trimmed[32:64]
        else:
            proto = f"RC-{best_enc[:3]}-{bl}b"
            mid = bl // 2
            serial_bits, hop_bits = trimmed[:mid], trimmed[mid:]

        pad = (4 - (len(trimmed) % 4)) % 4
        hex_full = f"{int(trimmed + '0' * pad, 2):0{(len(trimmed) + pad) // 4}X}"
        return {
            "protocol": proto, "encoding": best_enc, "bit_len": bl,
            "hex_full": hex_full[:24],
            "serial_hex": f"{int(serial_bits, 2):X}"[:12] if serial_bits else "",
            "hop_hex":    f"{int(hop_bits, 2):X}"[:12]    if hop_bits    else "",
        }


# ---------------------------------------------------------------------------
# CaptureItem (with dict serialization for persistence)
# ---------------------------------------------------------------------------

class CaptureItem:
    def __init__(self, name, source_file="Recorded", freq_hz=433920000,
                 preset="FuriHalSubGhzPresetOok650Async", protocol="RAW",
                 raw_data=None, hex_data="", radio_id=None,
                 decode_stage="RAW_CAPTURED"):
        self.name = name
        self.source_file = source_file
        self.freq_hz = freq_hz
        self.preset = preset
        self.protocol = protocol
        self.raw_data = list(raw_data or [])
        self.hex_data = hex_data
        self.hits = 1
        self.radio_id = radio_id or ("NRF1" if freq_hz >= 2_400_000_000 else "CC1101")
        self.decode_stage = decode_stage
        self.rc_info = None
        if self.raw_data and not self.is_nrf24:
            self.rc_info = RollingCodeDecoder.analyze(self.raw_data, self.freq_hz, self.preset)
            if self.rc_info:
                self.decode_stage = f"DECODED:{self.rc_info['protocol']}"
                if self.protocol in ("RAW", "RAW-TXT", "MD-RAW"):
                    self.protocol = self.rc_info["protocol"]
                if not self.hex_data:
                    self.hex_data = self.rc_info["hex_full"]

    @property
    def is_nrf24(self):
        return self.freq_hz >= 2_400_000_000 or "NRF" in self.protocol.upper()

    @property
    def band_label(self):
        return self.radio_id

    @property
    def freq_label(self):
        return f"{self.freq_hz / 1e6:.2f} MHz"

    @property
    def detail_label(self):
        hit = f" (x{self.hits})" if self.hits > 1 else ""
        if self.raw_data:
            dur = sum(abs(x) for x in self.raw_data) / 1000.0
            if self.rc_info and self.rc_info.get("hop_hex"):
                return (f"{len(self.raw_data)}p ({dur:.0f}ms) | "
                        f"ID:{self.rc_info['serial_hex']} HOP:{self.rc_info['hop_hex']}{hit}")
            return f"{len(self.raw_data)} pulses ({dur:.1f} ms){hit}"
        if self.hex_data:
            return f"PKT HEX: {self.hex_data[:24]}{hit}"
        return "Empty"

    def summary(self):
        return f"[{self.band_label}] {self.name} | {self.freq_label} | {self.protocol} | {self.detail_label}"

    def to_flipper_sub(self):
        lines = ["Filetype: Flipper SubGhz RAW File", "Version: 1",
                 f"Frequency: {self.freq_hz}", f"Preset: {self.preset}",
                 "Protocol: RAW"]
        for i in range(0, len(self.raw_data), 512):
            lines.append("RAW_Data: " + " ".join(str(x) for x in self.raw_data[i:i+512]))
        return "\n".join(lines) + "\n"

    def to_markdown_block(self):
        return (f"### {self.name}\n- Radio: {self.radio_id}\n"
                f"- Frequency: {self.freq_hz / 1e6:.2f} MHz\n"
                f"- Preset: {self.preset}\n- Protocol: {self.protocol}\n"
                f"- Stage: {self.decode_stage}\n- Data: `{self.hex_data}`\n")

    def to_dict(self):
        return {
            "name": self.name, "source_file": self.source_file,
            "freq_hz": self.freq_hz, "preset": self.preset,
            "protocol": self.protocol, "raw_data": self.raw_data,
            "hex_data": self.hex_data, "hits": self.hits,
            "radio_id": self.radio_id, "decode_stage": self.decode_stage,
        }

    @classmethod
    def from_dict(cls, d):
        obj = cls(d.get("name", "recall"), d.get("source_file", "restore"),
                  d.get("freq_hz", 433920000),
                  d.get("preset", "FuriHalSubGhzPresetOok650Async"),
                  d.get("protocol", "RAW"),
                  d.get("raw_data", []), d.get("hex_data", ""),
                  d.get("radio_id", None),
                  d.get("decode_stage", "RAW_CAPTURED"))
        obj.hits = d.get("hits", 1)
        return obj


# ---------------------------------------------------------------------------
# Visualizer (unchanged — good code)
# ---------------------------------------------------------------------------

class RFWaterfallCanvas(tk.Canvas):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg="#09090b", highlightthickness=1,
                         highlightbackground="#27272a", **kw)
        self.mode = "WAVE"
        self.wf_rows = []
        self.max_wf_rows = 26
        self.sub_density_rows = []
        self.max_sub_rows = 8
        self.current_pulses = []
        self.playhead_ratio = None
        self.status_text = "CC1101 // READY"
        self.stage_text = "IDLE"
        self.bind("<Configure>", lambda e: self.redraw())

    def set_waveform(self, pulses, label="CC1101", playhead=None,
                     push_density=False, stage="IDLE"):
        self.mode = "WAVE"
        self.current_pulses = pulses or []
        self.playhead_ratio = playhead
        self.status_text = label
        self.stage_text = stage
        if push_density and self.current_pulses:
            self._append_density_row(self.current_pulses)
        self.redraw()

    def _append_density_row(self, pulses):
        bins = 64
        sample = pulses[:2048]
        if not sample:
            return
        step = max(1, len(sample) // bins)
        row = []
        for i in range(0, len(sample), step):
            chunk = sample[i:i+step]
            high = sum(p for p in chunk if p > 0)
            tot = sum(abs(p) for p in chunk) or 1
            duty = high / tot
            row.append(3 if duty > 0.55 else 2 if duty > 0.25 else 1 if duty > 0.05 else 0)
        self.sub_density_rows.insert(0, row[:bins])
        if len(self.sub_density_rows) > self.max_sub_rows:
            self.sub_density_rows.pop()

    def set_playhead(self, ratio):
        self.playhead_ratio = ratio
        self.redraw()

    def push_waterfall_row(self, ch_intensities, label="2.4G", stage="SCAN"):
        self.mode = "WATERFALL"
        self.status_text = label
        self.stage_text = stage
        self.wf_rows.insert(0, ch_intensities[:126])
        if len(self.wf_rows) > self.max_wf_rows:
            self.wf_rows.pop()
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = max(self.winfo_width(), 680)
        h = max(self.winfo_height(), 120)
        for gx in range(0, w, 40):
            self.create_line(gx, 0, gx, h, fill="#141418")
        if self.mode == "WATERFALL":
            self._draw_waterfall(w, h)
        else:
            self._draw_waveform(w, h)
        self.create_rectangle(6, 4, 340, 20, fill="#111115", outline="#27272a")
        self.create_text(12, 12, text=self.status_text, anchor="w",
                         fill="#ef4444", font=("Monospace", 8, "bold"))
        self.create_rectangle(w - 280, 4, w - 6, 20, fill="#111115", outline="#27272a")
        self.create_text(w - 12, 12, text=self.stage_text, anchor="e",
                         fill="#38bdf8", font=("Monospace", 8, "bold"))

    def _draw_waveform(self, w, h):
        pulses = self.current_pulses
        split = h - 30
        self.create_line(0, split, w, split, fill="#1f1f26")
        if not pulses:
            self.create_text(w // 2, (split + 20) // 2,
                             text="NO PULSES BUFFERED",
                             fill="#52525b", font=("Monospace", 8, "bold"))
            return
        sample = pulses[:4000]
        total = sum(abs(p) for p in sample) or 1
        pad = 8
        dw = w - pad * 2
        y_hi, y_lo = 24, split - 6
        coords, acc, last = [], 0, y_lo
        for p in sample:
            x0 = pad + (acc / total) * dw
            acc += abs(p)
            x1 = pad + (acc / total) * dw
            y = y_hi if p > 0 else y_lo
            if coords:
                coords.extend([x0, last, x0, y])
            else:
                coords.extend([x0, y])
            coords.extend([x1, y])
            last = y
        if len(coords) >= 4:
            self.create_line(*coords, fill="#ef4444", width=1.5)
        if self.sub_density_rows:
            top = split + 2
            wf_h = max(h - top - 12, 8)
            rh = wf_h / self.max_sub_rows
            pal = {1: "#450a0a", 2: "#dc2626", 3: "#f87171"}
            for r, row in enumerate(self.sub_density_rows):
                y0 = top + r * rh
                y1 = y0 + rh
                cw = dw / max(len(row), 1)
                for c, lvl in enumerate(row):
                    if lvl > 0:
                        x0 = pad + c * cw
                        self.create_rectangle(x0, y0, x0 + cw, y1,
                                              fill=pal.get(lvl, "#ef4444"), outline="")
        dur = sum(abs(p) for p in pulses) / 1000.0
        self.create_text(w - 10, h - 7,
                         text=f"{len(pulses)} PULSES | {dur:.1f} ms",
                         anchor="e", fill="#a1a1aa",
                         font=("Monospace", 7, "bold"))
        if self.playhead_ratio is not None:
            px = pad + max(0.0, min(1.0, self.playhead_ratio)) * dw
            self.create_line(px, 20, px, split, fill="#38bdf8", width=2)

    def _draw_waterfall(self, w, h):
        pad = 10
        top = 24
        bot = h - 15
        dw = w - pad * 2
        dh = max(bot - top, 20)
        cw = dw / 126.0
        rh = dh / self.max_wf_rows
        pal = {0: "#121216", 1: "#0369a1", 2: "#38bdf8", 3: "#ef4444"}
        for r, row in enumerate(self.wf_rows):
            y0 = top + r * rh
            y1 = y0 + rh
            for ch, lvl in enumerate(row):
                if lvl <= 0:
                    continue
                x0 = pad + ch * cw
                self.create_rectangle(x0, y0, x0 + max(cw, 1.5), y1,
                                      fill=pal.get(lvl, "#ef4444"), outline="")
        for ch in (0, 26, 50, 76, 100, 125):
            self.create_text(pad + ch * cw, h - 7, text=f"{2400 + ch}",
                             fill="#71717a", font=("Monospace", 7))


# ---------------------------------------------------------------------------
# IR bar (unchanged — self-contained, useful)
# ---------------------------------------------------------------------------

import json as _ir_json

def parse_ir_file(path):
    sigs, cur = [], None
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = [x.strip() for x in line.split(":", 1)]
            if k == "name":
                if cur:
                    sigs.append(cur)
                cur = {"name": v, "type": "raw", "frequency": 38000,
                       "duty_cycle": 0.33, "data": []}
            elif cur is not None:
                if k == "type":
                    cur["type"] = v.lower()
                elif k == "frequency":
                    try: cur["frequency"] = int(float(v))
                    except ValueError: pass
                elif k == "duty cycle":
                    try: cur["duty_cycle"] = float(v)
                    except ValueError: pass
                elif k == "data":
                    cur["data"] = [int(t) for t in v.replace(",", " ").split()
                                   if t.lstrip("-").isdigit()]
                else:
                    cur[k] = v
    if cur:
        sigs.append(cur)
    return sigs

def export_ir_file(path, sigs):
    lines = ["Filetype: IR signals file", "Version: 1"]
    for i, sig in enumerate(sigs, 1):
        st = sig.get("type", "raw")
        lines += [f"# Signal {i}", f"name: {sig.get('name', f'SIG_{i}')}", f"type: {st}"]
        if st == "raw":
            lines += [f"frequency: {int(sig.get('frequency', 38000))}",
                      f"duty cycle: {sig.get('duty_cycle', 0.33)}",
                      "data: " + " ".join(str(int(x)) for x in sig.get("data", []))]
        else:
            for f in ("protocol", "address", "command"):
                if f in sig:
                    lines.append(f"{f}: {sig[f]}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).strip() + "\n")

class IrBlasterBar(tk.Frame):
    def __init__(self, parent, port_getter=None, **kw):
        super().__init__(parent, bg="#111115", highlightbackground="#dc2626",
                         highlightthickness=1, pady=3, padx=6, **kw)
        self.port_getter = port_getter
        self.signals = []
        self.recording = False
        self._stop = threading.Event()

        self.drop_lbl = tk.Label(self, text="IR // DROP .IR OR LOAD",
                                 bg="#18181b", fg="#f4f4f5",
                                 font=("Monospace", 8, "bold"),
                                 padx=8, pady=2, cursor="hand2")
        self.drop_lbl.pack(side="left", padx=(0, 4))
        self.drop_lbl.bind("<Button-1>", lambda _e: self.load_dialog())

        self.sig_var = tk.StringVar(value="[NO .IR LOADED]")
        self.sig_menu = tk.OptionMenu(self, self.sig_var, "[NO .IR LOADED]")
        self.sig_menu.config(bg="#18181b", fg="#e4e4e7", activebackground="#27272a",
                             activeforeground="white", highlightthickness=0, bd=0,
                             font=("Monospace", 8, "bold"), padx=6, pady=2)
        self.sig_menu["menu"].config(bg="#18181b", fg="#e4e4e7",
                                     activebackground="#dc2626",
                                     activeforeground="white",
                                     font=("Monospace", 8))
        self.sig_menu.pack(side="left", padx=3)

        tk.Button(self, text="PLAY", bg="#14532d", fg="#4ade80",
                  activebackground="#166534", activeforeground="white",
                  relief="flat", padx=8, pady=2,
                  font=("Monospace", 8, "bold"), command=self.play_sig).pack(side="left", padx=2)

        self.btn_rec = tk.Button(self, text="REC", bg="#3f1111", fg="#f87171",
                                 activebackground="#dc2626", activeforeground="white",
                                 relief="flat", padx=8, pady=2,
                                 font=("Monospace", 8, "bold"), command=self.toggle_rec)
        self.btn_rec.pack(side="left", padx=2)

        tk.Button(self, text="SAVE", bg="#1e293b", fg="#38bdf8",
                  activebackground="#0284c7", activeforeground="white",
                  relief="flat", padx=8, pady=2,
                  font=("Monospace", 8, "bold"), command=self.save_dialog).pack(side="left", padx=2)

        self.info_lbl = tk.Label(self, text="IDLE", bg="#111115", fg="#a1a1aa",
                                 font=("Monospace", 8, "bold"))
        self.info_lbl.pack(side="right", padx=6)

        if HAS_DND:
            for w in (self, self.drop_lbl):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass

    def _on_drop(self, ev):
        raw = (ev.data or "").strip()
        p = raw[1:-1] if raw.startswith("{") and raw.endswith("}") else raw.split()[0] if raw else ""
        if p:
            self.load_file(p)

    def load_dialog(self):
        p = filedialog.askopenfilename(title="Select .ir File",
                                       filetypes=[("IR Files", "*.ir"), ("All", "*.*")])
        if p:
            self.load_file(p)

    def load_file(self, p):
        if not p.lower().endswith(".ir"):
            self.info_lbl.config(text="ONLY .IR", fg="#f87171")
            return
        parsed = parse_ir_file(p)
        if not parsed:
            self.info_lbl.config(text="EMPTY", fg="#f87171")
            return
        self.signals = parsed
        self._refresh()
        self.info_lbl.config(text=f"LOADED {len(parsed)} ({os.path.basename(p)})", fg="#4ade80")

    def _refresh(self, sel=None):
        m = self.sig_menu["menu"]
        m.delete(0, "end")
        names = [x.get("name", f"SIG_{i+1}") for i, x in enumerate(self.signals)] or ["[NO .IR LOADED]"]
        for n in names:
            m.add_command(label=n, command=lambda v=n: self.sig_var.set(v))
        self.sig_var.set(sel if sel in names else names[0])

    def _current(self):
        for x in self.signals:
            if x.get("name") == self.sig_var.get():
                return x
        return self.signals[0] if self.signals else None

    def _port(self):
        if callable(self.port_getter):
            try:
                p = self.port_getter()
                if p: return p
            except Exception:
                pass
        for c in ("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0", "/dev/ttyUSB1"):
            if os.path.exists(c):
                return c
        return None

    def play_sig(self):
        sig = self._current()
        if not sig:
            self.info_lbl.config(text="NO SIGNAL", fg="#facc15")
            return
        port = self._port()
        pkt = {"cmd": "ir_tx", "name": sig.get("name", "SIG"),
               "type": sig.get("type", "raw"),
               "frequency": int(sig.get("frequency", 38000)),
               "duty_cycle": float(sig.get("duty_cycle", 0.33)),
               "data": sig.get("data", [])}
        for k in ("protocol", "address", "command"):
            if k in sig: pkt[k] = sig[k]
        if not port:
            self.info_lbl.config(text="NO PORT", fg="#facc15")
            return
        try:
            with serial.Serial(port, 115200, timeout=0.5) as ser:
                ser.write((_ir_json.dumps(pkt) + "\n").encode("utf-8"))
            print(f"ROBOT_EVENT:TX:{sig.get('name','SIG')}", flush=True)
            self.info_lbl.config(text=f"TX {pkt['name']}", fg="#4ade80")
        except Exception as e:
            self.info_lbl.config(text=f"TX ERR: {str(e)[:18]}", fg="#f87171")

    def toggle_rec(self):
        if not self.recording:
            self.recording = True
            self._stop.clear()
            self.btn_rec.config(text="STOP", bg="#dc2626", fg="white")
            self.info_lbl.config(text="REC IR...", fg="#f87171")
            threading.Thread(target=self._rec_job, daemon=True).start()
        else:
            self.recording = False
            self._stop.set()
            self.btn_rec.config(text="REC", bg="#3f1111", fg="#f87171")
            self.info_lbl.config(text="REC STOP", fg="#a1a1aa")

    def _rec_job(self):
        pulses, port, t0 = [], self._port(), time.monotonic()
        if port:
            try:
                with serial.Serial(port, 115200, timeout=0.1) as ser:
                    ser.write(b'{"cmd":"ir_rx_start"}\n')
                    while not self._stop.is_set() and (time.monotonic() - t0) < 10.0:
                        ln = ser.readline().decode("utf-8", errors="ignore").strip()
                        if ln:
                            pulses += [int(t) for t in ln.replace(",", " ").split()
                                       if t.lstrip("-").isdigit()]
                    ser.write(b'{"cmd":"ir_rx_stop"}\n')
            except Exception:
                pass
        if not pulses:
            pulses = [9000, -4500, 560, -560, 560, -1690, 560, -560, 560, -1690]
        name = f"IR_REC_{len(self.signals)+1}"
        self.signals.append({"name": name, "type": "raw", "frequency": 38000,
                             "duty_cycle": 0.33, "data": pulses})
        self.recording = False
        self._stop.clear()
        self.after(0, lambda: self.btn_rec.config(text="REC", bg="#3f1111", fg="#f87171"))
        self.after(0, lambda: self._refresh(name))
        self.after(0, lambda: self.info_lbl.config(text=f"SAVED {len(pulses)}", fg="#4ade80"))

    def save_dialog(self):
        if not self.signals:
            self.info_lbl.config(text="EMPTY", fg="#facc15")
            return
        p = filedialog.asksaveasfilename(defaultextension=".ir",
                                         initialfile="jitter_ir_export.ir",
                                         filetypes=[("IR Files", "*.ir")])
        if p:
            export_ir_file(p, self.signals)
            self.info_lbl.config(text=f"EXPORTED", fg="#38bdf8")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class JitterDeck4:
    def __init__(self, root):
        self.root = root
        self.root.title("JITTERDECK 4 // RF Workbench")
        self.root.geometry("780x820")
        self.root.configure(bg="#09090b")

        ensure_persist()

        self.queue = []
        self.ser = None
        self.ser_lock = threading.Lock()
        self.is_playing = False
        self.is_recording = False
        self.is_sweeping = False
        self.is_nrf_scanning = False
        self.nrf3_ir_mode = False
        self.rec_pulses = []
        self.rec_freq_hz = 433920000
        self.rec_preset = "FuriHalSubGhzPresetOok650Async"
        self.rec_count = 0
        self.last_nrf_hex = ""
        self.last_nrf_ts = 0.0
        self.sweep_hits = []

        self.radio_cards = {}
        self.setup_styles()
        self.build_ui()
        self.load_bank()
        self.refresh_ports(auto_connect=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground="#141418", background="#1f1f26",
                        foreground="#e4e4e7", arrowcolor="#ef4444",
                        bordercolor="#27272a")
        style.configure("Deck.Treeview", background="#121216", foreground="#f4f4f5",
                        fieldbackground="#121216", borderwidth=0, rowheight=21,
                        font=("Monospace", 9))
        style.configure("Deck.Treeview.Heading", background="#18181b",
                        foreground="#a1a1aa", relief="flat",
                        font=("Monospace", 8, "bold"))
        style.map("Deck.Treeview", background=[("selected", "#dc2626")],
                  foreground=[("selected", "white")])
        self.root.option_add("*TCombobox*Listbox.background", "#141418")
        self.root.option_add("*TCombobox*Listbox.foreground", "#e4e4e7")
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#dc2626")

    # ---------------- UI build ----------------
    def build_ui(self):
        # Header
        hdr = tk.Frame(self.root, bg="#111115", height=34,
                       highlightbackground="#27272a", highlightthickness=1)
        hdr.pack(fill="x", padx=10, pady=(8, 3))
        hdr.pack_propagate(False)
        tk.Label(hdr, text="JITTERDECK 4", fg="#ef4444", bg="#111115",
                 font=("Monospace", 12, "bold")).pack(side="left", padx=10)
        self.status_badge = tk.Label(hdr, text="OFFLINE", fg="#a1a1aa",
                                     bg="#1f1f26", font=("Monospace", 9, "bold"),
                                     padx=8, pady=2)
        self.status_badge.pack(side="right", padx=10)

        # Link bar
        link = tk.Frame(self.root, bg="#09090b")
        link.pack(fill="x", padx=10, pady=2)
        tk.Label(link, text="PORT:", fg="#a1a1aa", bg="#09090b",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.port_combo = ttk.Combobox(link, width=15, state="readonly",
                                       font=("Monospace", 9))
        self.port_combo.pack(side="left", padx=4)
        tk.Label(link, text="BAUD:", fg="#a1a1aa", bg="#09090b",
                 font=("Monospace", 9, "bold")).pack(side="left", padx=(2, 0))
        self.baud_combo = ttk.Combobox(link, width=7,
                                       values=["115200", "38400", "9600"],
                                       state="readonly", font=("Monospace", 9))
        self.baud_combo.set("115200")
        self.baud_combo.pack(side="left", padx=4)
        tk.Button(link, text="↻", command=lambda: self.refresh_ports(auto_connect=False),
                  bg="#18181b", fg="#d4d4d8", activebackground="#27272a",
                  activeforeground="white", relief="flat",
                  font=("Monospace", 10, "bold"), width=3).pack(side="left", padx=2)
        self.conn_btn = tk.Button(link, text="CONNECT", command=self.toggle_connection,
                                  bg="#dc2626", fg="white", activebackground="#b91c1c",
                                  activeforeground="white", relief="flat",
                                  font=("Monospace", 9, "bold"), padx=10, pady=2)
        self.conn_btn.pack(side="right")
        tk.Button(link, text="PROBE", command=self.probe_radios,
                  bg="#18181b", fg="#4ade80", activebackground="#14532d",
                  activeforeground="white", highlightbackground="#22c55e",
                  highlightthickness=1, relief="flat",
                  font=("Monospace", 8, "bold"), padx=8, pady=2).pack(side="right", padx=4)
        tk.Button(link, text="🍔 FOOD", command=self.launch_jitterfood,
                  bg="#18181b", fg="#f59e0b", activebackground="#92400e",
                  activeforeground="white", highlightbackground="#f59e0b",
                  highlightthickness=1, relief="flat",
                  font=("Monospace", 8, "bold"), padx=8, pady=2).pack(side="right", padx=2)
        tk.Button(link, text="🍦 ICE CREAM", command=self.launch_icecream,
                  bg="#18181b", fg="#f472b6", activebackground="#9d174d",
                  activeforeground="white", highlightbackground="#f472b6",
                  highlightthickness=1, relief="flat",
                  font=("Monospace", 8, "bold"), padx=8, pady=2).pack(side="right", padx=2)

        # Radio status cards
        cards = tk.Frame(self.root, bg="#09090b")
        cards.pack(fill="x", padx=10, pady=(2, 3))
        for i, rn in enumerate(("CC1101", "NRF1", "NRF2", "NRF3")):
            spec = RADIO_SPECS[rn]
            c = tk.Frame(cards, bg="#111115", highlightbackground="#27272a",
                         highlightthickness=1, padx=6, pady=4)
            c.pack(side="left", fill="both", expand=True, padx=(0 if i == 0 else 4, 0))
            top = tk.Frame(c, bg="#111115"); top.pack(fill="x")
            color = "#f87171" if rn == "CC1101" else "#38bdf8"
            tk.Label(top, text=rn, fg=color, bg="#111115",
                     font=("Monospace", 9, "bold")).pack(side="left")
            st = tk.Label(top, text="OFFLINE", fg="#71717a", bg="#18181b",
                          font=("Monospace", 7, "bold"), padx=4)
            st.pack(side="right")
            tk.Label(c, text=spec["pins"], fg="#a1a1aa", bg="#111115",
                     font=("Monospace", 7), anchor="w").pack(fill="x", pady=(2, 0))
            tk.Label(c, text=spec["band"], fg="#71717a", bg="#111115",
                     font=("Monospace", 7), anchor="w").pack(fill="x")
            self.radio_cards[rn] = {"frame": c, "status": st}
            if rn == "NRF3":
                self.nrf3_toggle_btn = tk.Button(c, text="⇄ IR SHIELD",
                                                  command=self.toggle_nrf3_ir_mode,
                                                  bg="#18181b", fg="#f59e0b",
                                                  activebackground="#92400e",
                                                  activeforeground="white",
                                                  highlightbackground="#f59e0b",
                                                  highlightthickness=1, relief="flat",
                                                  font=("Monospace", 7, "bold"),
                                                  padx=4, pady=1, cursor="hand2")
                self.nrf3_toggle_btn.pack(fill="x", pady=(3, 0))

        # === INGEST panel ===
        ingest = tk.LabelFrame(self.root, text=" INGEST ", bg="#111115",
                               fg="#ef4444", font=("Monospace", 8, "bold"),
                               highlightbackground="#27272a", highlightthickness=1,
                               padx=8, pady=4)
        ingest.pack(fill="x", padx=10, pady=2)

        rx = tk.Frame(ingest, bg="#111115"); rx.pack(fill="x")
        tk.Label(rx, text="FREQ:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.freq_combo = ttk.Combobox(rx, width=8, values=COMMON_FREQS,
                                       font=("Monospace", 9))
        self.freq_combo.set("433.92")
        self.freq_combo.pack(side="left", padx=4)
        tk.Label(rx, text="MOD:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold")).pack(side="left", padx=(2, 0))
        self.preset_combo = ttk.Combobox(rx, width=18,
                                         values=list(PRESET_MAP.keys()),
                                         state="readonly", font=("Monospace", 9))
        self.preset_combo.current(0)
        self.preset_combo.pack(side="left", padx=4)

        self.rec_btn = tk.Button(rx, text="● REC", command=self.toggle_record,
                                  bg="#18181b", fg="#ef4444", activebackground="#dc2626",
                                  activeforeground="white", highlightbackground="#ef4444",
                                  highlightthickness=1, relief="flat",
                                  font=("Monospace", 9, "bold"), padx=7, pady=2)
        self.rec_btn.pack(side="right", padx=(4, 0))
        tk.Button(rx, text="CLR", command=self.clear_live_rx,
                  bg="#18181b", fg="#facc15", activebackground="#ca8a04",
                  activeforeground="white", highlightbackground="#facc15",
                  highlightthickness=1, relief="flat",
                  font=("Monospace", 9, "bold"), padx=6, pady=2).pack(side="right", padx=4)
        tk.Button(rx, text="SWEEP", command=self.start_sweep,
                  bg="#18181b", fg="#a855f7", activebackground="#7e22ce",
                  activeforeground="white", highlightbackground="#a855f7",
                  highlightthickness=1, relief="flat",
                  font=("Monospace", 9, "bold"), padx=6, pady=2).pack(side="right", padx=4)

        # === ANALYZE panel ===
        analyze = tk.LabelFrame(self.root, text=" ANALYZE ", bg="#111115",
                                fg="#38bdf8", font=("Monospace", 8, "bold"),
                                highlightbackground="#27272a", highlightthickness=1,
                                padx=8, pady=4)
        analyze.pack(fill="x", padx=10, pady=2)

        ar = tk.Frame(analyze, bg="#111115"); ar.pack(fill="x")
        tk.Label(ar, text="NRF:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.nrf_unit_combo = ttk.Combobox(ar, width=5,
                                           values=["NRF1", "NRF2", "NRF3"],
                                           state="readonly", font=("Monospace", 9, "bold"))
        self.nrf_unit_combo.current(0)
        self.nrf_unit_combo.pack(side="left", padx=4)
        self.nrf_unit_combo.bind("<<ComboboxSelected>>", self.select_nrf_unit)
        self.nrf_scan_btn = tk.Button(ar, text="OCCUPANCY SCAN", command=self.toggle_nrf_scan,
                                      bg="#18181b", fg="#38bdf8",
                                      activebackground="#0284c7", activeforeground="white",
                                      highlightbackground="#38bdf8", highlightthickness=1,
                                      relief="flat", font=("Monospace", 9, "bold"),
                                      padx=8, pady=2)
        self.nrf_scan_btn.pack(side="left", padx=4)
        tk.Button(ar, text="SNAPSHOT", command=self.snapshot_scan,
                  bg="#18181b", fg="#a1a1aa", activebackground="#27272a",
                  activeforeground="white", relief="flat",
                  font=("Monospace", 9, "bold"), padx=8, pady=2).pack(side="left", padx=4)

        # === EXPERIMENTAL (reserved, empty) ===
        exp = tk.LabelFrame(self.root, text=" EXPERIMENTAL ", bg="#111115",
                            fg="#52525b", font=("Monospace", 8, "bold"),
                            highlightbackground="#27272a", highlightthickness=1,
                            padx=8, pady=8)
        exp.pack(fill="x", padx=10, pady=2)
        tk.Label(exp, text="Reserved for future legal features (spectrum mapping, "
                           "fingerprinting, adversarial-capture replay to authorized devices)",
                 fg="#52525b", bg="#111115", font=("Monospace", 8, "italic")).pack()

        # === DROP + BANK toolbar ===
        drop = tk.Frame(self.root, bg="#121216", height=42,
                        highlightbackground="#dc2626", highlightthickness=2, cursor="hand2")
        drop.pack(fill="x", padx=10, pady=2)
        drop.pack_propagate(False)
        tk.Label(drop, text="DROP .SUB / .RAW / .MD FILES HERE" if HAS_DND
                           else "CLICK TO LOAD .SUB / .RAW / .MD FILES",
                 fg="#f4f4f5", bg="#121216", font=("Monospace", 9, "bold")).pack(expand=True)
        for w in (drop,) + tuple(drop.winfo_children()):
            w.bind("<Button-1>", lambda e: self.browse_files())
            if HAS_DND:
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self.handle_drop)
                except Exception:
                    pass

        bank_bar = tk.Frame(self.root, bg="#09090b")
        bank_bar.pack(fill="x", padx=10, pady=(2, 2))
        tk.Label(bank_bar, text="BANK:", fg="#a1a1aa", bg="#09090b",
                 font=("Monospace", 9, "bold")).pack(side="left")
        tk.Label(bank_bar, text="RAW µs/S:", fg="#71717a", bg="#09090b",
                 font=("Monospace", 8, "bold")).pack(side="left", padx=(10, 2))
        self.raw_rate_combo = ttk.Combobox(bank_bar, width=4,
                                           values=["1", "2", "4", "10", "20", "50"],
                                           state="readonly", font=("Monospace", 8))
        self.raw_rate_combo.set("10")
        self.raw_rate_combo.pack(side="left")
        tk.Button(bank_bar, text="💾 EXPORT", command=self.save_selected_capture,
                  bg="#1f2937", fg="#38bdf8", activebackground="#0284c7",
                  activeforeground="white", relief="flat",
                  font=("Monospace", 8, "bold"), padx=8).pack(side="right", padx=4)
        tk.Button(bank_bar, text="DEL", command=self.delete_selected,
                  bg="#18181b", fg="#f87171", activebackground="#27272a",
                  activeforeground="white", relief="flat",
                  font=("Monospace", 8, "bold"), padx=6).pack(side="right", padx=2)
        tk.Button(bank_bar, text="RENAME", command=self.rename_selected,
                  bg="#18181b", fg="#d4d4d8", activebackground="#27272a",
                  activeforeground="white", relief="flat",
                  font=("Monospace", 8, "bold"), padx=6).pack(side="right", padx=2)
        tk.Button(bank_bar, text="CLEAR", command=self.clear_queue,
                  bg="#18181b", fg="#a1a1aa", activebackground="#27272a",
                  activeforeground="white", relief="flat",
                  font=("Monospace", 8, "bold"), padx=6).pack(side="right", padx=2)

        # Bank tree
        tf = tk.Frame(self.root, bg="#27272a", padx=1, pady=1)
        tf.pack(fill="x", padx=10, pady=2)
        cols = ("radio", "name", "freq", "proto", "detail")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=4,
                                 style="Deck.Treeview", selectmode="browse")
        for c, t, w, a in (("radio", "RADIO", 68, "center"),
                           ("name", "NAME", 180, "w"),
                           ("freq", "FREQ", 95, "center"),
                           ("proto", "PROTO", 100, "center"),
                           ("detail", "DETAIL", 260, "w")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c == "detail"))
        self.tree.tag_configure("SUBG", foreground="#fca5a5")
        self.tree.tag_configure("NRF", foreground="#7dd3fc")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_capture)
        if HAS_DND:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self.handle_drop)

        # Visualizer
        self.visualizer = RFWaterfallCanvas(self.root, height=120)
        self.visualizer.pack(fill="x", padx=10, pady=3)

        # IR bar
        self.ir_blaster_bar = IrBlasterBar(self.root,
                                            port_getter=lambda: self.port_combo.get() or None)
        self.ir_blaster_bar.pack(fill="x", padx=10, pady=(0, 3))

        # Console
        self.console = tk.Text(self.root, height=3, bg="#0f0f13", fg="#d4d4d8",
                               insertbackground="#ef4444", font=("Monospace", 9),
                               relief="flat", highlightbackground="#27272a",
                               highlightthickness=1, padx=8, pady=4)
        self.console.pack(fill="both", expand=True, padx=10, pady=3)

        # Footer controls
        ctrl = tk.Frame(self.root, bg="#09090b")
        ctrl.pack(fill="x", padx=10, pady=(2, 8))
        tk.Label(ctrl, text="REPEATS:", fg="#a1a1aa", bg="#09090b",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.repeat_spin = tk.Spinbox(ctrl, from_=1, to=50, width=5,
                                      bg="#141418", fg="#f4f4f5",
                                      buttonbackground="#27272a", relief="flat",
                                      font=("Monospace", 9))
        self.repeat_spin.pack(side="left", padx=6)
        self.play_btn = tk.Button(ctrl, text="▶ PLAY .SUB", command=self.trigger_play,
                                  bg="#dc2626", fg="white", activebackground="#ef4444",
                                  activeforeground="white", relief="flat",
                                  font=("Monospace", 12, "bold"), padx=20, pady=8)
        self.play_btn.pack(side="right", padx=(4, 0))
        tk.Button(ctrl, text="LOAD .SUB", command=self._quick_load_sub,
                  bg="#1e293b", fg="#38bdf8", activebackground="#0284c7",
                  activeforeground="white", relief="flat",
                  font=("Monospace", 9, "bold"), padx=10, pady=8
                  ).pack(side="right", padx=4)

        self.log("[*] JitterDeck 4 online. Type: RX only — no jamming.")
        self.log(f"[*] Capture bank: {BANK_PATH}")

    def set_radio_status(self, name, status, ok=None):
        if name not in self.radio_cards:
            return
        c = self.radio_cards[name]
        if ok is True:
            fg, bg, bd = "#4ade80", "#14291d", "#22c55e"
        elif ok is False:
            fg, bg, bd = "#f87171", "#3f1111", "#ef4444"
        elif ok == "WARN":
            fg, bg, bd = "#facc15", "#292214", "#eab308"
        else:
            fg, bg, bd = "#71717a", "#18181b", "#27272a"
        c["status"].config(text=status[:14], fg=fg, bg=bg)
        c["frame"].config(highlightbackground=bd)

    def log(self, msg):
        self.console.insert(tk.END, msg + "\n")
        n = int(self.console.index("end-1c").split(".")[0])
        if n > 500:
            self.console.delete("1.0", f"{n-500}.0")
        self.console.see(tk.END)

    # ---------------- persistence ----------------
    def load_bank(self):
        if not os.path.isfile(BANK_PATH):
            return
        try:
            with open(BANK_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("items", []):
                self.queue.append(CaptureItem.from_dict(d))
            self._refresh_tree()
            self.log(f"[*] Restored {len(self.queue)} captures from disk.")
        except Exception as e:
            self.log(f"[!] Bank load failed: {e}")

    def save_bank(self):
        try:
            with open(BANK_PATH, "w", encoding="utf-8") as f:
                json.dump({"items": [x.to_dict() for x in self.queue]},
                          f, indent=2, sort_keys=True)
        except Exception as e:
            self.log(f"[!] Bank save failed: {e}")

    # ---------------- ports / link ----------------
    def refresh_ports(self, auto_connect=False):
        ports = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
        self.port_combo["values"] = ports
        if ports:
            self.port_combo.current(0)
            self.log(f"[*] Ports: {', '.join(ports)}")
            if auto_connect and (not self.ser or not self.ser.is_open):
                self.root.after(150, self.toggle_connection)
        else:
            self.port_combo.set("")
            self.log("[!] No serial ports detected.")

    def toggle_connection(self):
        if self.is_recording:
            self.stop_record()
        self.is_sweeping = False
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self.conn_btn.config(text="CONNECT", bg="#dc2626")
                self.status_badge.config(text="OFFLINE", fg="#a1a1aa", bg="#1f1f26")
                for rn in RADIO_SPECS:
                    self.set_radio_status(rn, "OFFLINE")
                self.nrf3_ir_mode = False
                self._apply_nrf3_ir_mode_ui()
                self.log("[*] Link closed.")
                return
            port = self.port_combo.get()
            if not port:
                messagebox.showerror("No Port", "Select a serial port first.")
                return
            try:
                self.ser = serial.Serial(port, int(self.baud_combo.get()), timeout=0.35)
                self.ser.dtr = False
                self.ser.rts = False
                time.sleep(0.25)
                self.ser.reset_input_buffer()
                self.ser.write(b"V")
                resp = self.ser.readline().decode("utf-8", errors="ignore").strip()
                self.conn_btn.config(text="DISCONNECT", bg="#27272a")
                if resp:
                    self.status_badge.config(text=f"ONLINE: {resp[:24]}",
                                             fg="#4ade80", bg="#14291d")
                    self.log(f"[+] Connected -> {resp}")
                    for rn in ("CC1101", "NRF1", "NRF2"):
                        self.set_radio_status(rn, "READY", ok=True)
                    self.nrf3_ir_mode = False
                    self._apply_nrf3_ir_mode_ui()
                else:
                    self.status_badge.config(text=f"OPEN ({port})",
                                             fg="#facc15", bg="#292214")
                    self.log(f"[+] Opened {port} (no V banner).")
            except Exception as e:
                self.ser = None
                messagebox.showerror("Serial Error", f"{port}:\n{e}")

    def probe_radios(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Offline", "Connect first.")
            return
        all_lines = []
        with self.ser_lock:
            old = self.ser.timeout
            try:
                self.ser.timeout = 0.25
                for cmd in (b"V", b"R"):
                    self.ser.reset_input_buffer()
                    self.ser.write(cmd)
                    deadline = time.monotonic() + 1.2
                    while time.monotonic() < deadline:
                        raw = self.ser.readline()
                        if raw:
                            line = raw.decode("utf-8", errors="replace").strip()
                            if line:
                                all_lines.append(line)
                        elif all_lines:
                            break
            except Exception as e:
                self.log(f"[!] Probe error: {e}")
            finally:
                self.ser.timeout = old
        for line in all_lines:
            up = line.upper()
            if "CC1101" in up:
                ok = not any(w in up for w in ("FAIL", "ERR", "NONE", "OFF"))
                self.set_radio_status("CC1101", "ONLINE" if ok else "FAULT", ok=ok)
            for u in ("NRF1", "NRF2", "NRF3"):
                if u in up:
                    ok = not any(w in up for w in ("FAIL", "ERR", "NONE", "OFF"))
                    self.set_radio_status(u, "ONLINE" if ok else "FAULT", ok=ok)
            self.log(f"[PROBE] {line}")

    # ---------------- NRF3 / IR ----------------
    def toggle_nrf3_ir_mode(self):
        if self.is_recording or self.is_playing:
            return
        if not self.ser or not self.ser.is_open:
            return
        target = not self.nrf3_ir_mode
        with self.ser_lock:
            try:
                old = self.ser.timeout
                self.ser.timeout = 0.3
                self.ser.reset_input_buffer()
                self.ser.write(bytes([ord("G"), 1 if target else 0]))
                ack = self.ser.read(1)
                self.ser.timeout = old
            except Exception as e:
                self.log(f"[!] G failed: {e}")
                return
        if ack != b"K":
            self.log(f"[!] No ACK for G (got {ack!r})")
            return
        self.nrf3_ir_mode = target
        self._apply_nrf3_ir_mode_ui()
        self.log(f"[*] NRF3 mode -> {'IR SHIELD' if target else 'RADIO'}")

    def _apply_nrf3_ir_mode_ui(self):
        vals = list(self.nrf_unit_combo["values"])
        if self.nrf3_ir_mode:
            self.nrf3_toggle_btn.config(text="⇄ NRF3 RADIO", fg="#38bdf8",
                                         activebackground="#0c4a6e",
                                         highlightbackground="#38bdf8")
            self.set_radio_status("NRF3", "IR SHIELD", ok="WARN")
            if "NRF3" in vals:
                vals.remove("NRF3")
                self.nrf_unit_combo["values"] = vals
            if self.nrf_unit_combo.get() == "NRF3":
                self.nrf_unit_combo.current(0)
        else:
            self.nrf3_toggle_btn.config(text="⇄ IR SHIELD", fg="#f59e0b",
                                         activebackground="#92400e",
                                         highlightbackground="#f59e0b")
            self.set_radio_status("NRF3", "SPI/CE READY", ok="WARN")
            if "NRF3" not in vals:
                vals.append("NRF3")
                self.nrf_unit_combo["values"] = vals

    def select_nrf_unit(self, _evt=None):
        if self.is_recording or self.is_playing:
            return
        unit = self.nrf_unit_combo.get()
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                idx = self.nrf_unit_combo.current()
                self.ser.write(bytes([ord("U"), idx]))
                self.ser.read(1)
                self.log(f"[*] Active 2.4G: {unit}")

    # ---------------- RF record ----------------
    def toggle_record(self):
        if self.is_recording:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        if self.is_playing:
            return
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Offline", "Connect first.")
            return
        try:
            freq_hz = int(float(self.freq_combo.get()) * 1_000_000)
        except ValueError:
            messagebox.showerror("Bad Freq", "Invalid frequency.")
            return
        self.rec_freq_hz = freq_hz
        self.rec_preset = PRESET_MAP.get(self.preset_combo.get(),
                                          "FuriHalSubGhzPresetOok650Async")
        self.is_recording = True
        self.rec_pulses = []
        self.rec_btn.config(text="■ STOP", bg="#dc2626", fg="white")
        self.play_btn.config(state="disabled")
        self.status_badge.config(text=f"REC @ {freq_hz/1e6:.2f} MHz",
                                 fg="#f87171", bg="#3f1111")
        if freq_hz >= 2_400_000_000:
            ch = max(0, min(125, int(freq_hz / 1e6 - 2400)))
            self.visualizer.push_waterfall_row(
                [2 if i == ch else 0 for i in range(126)],
                f"SNIFF CH{ch}", stage="PACKET RX")
        else:
            self.visualizer.set_waveform([],
                f"LIVE RX {freq_hz/1e6:.2f} MHz", stage="EDGE WAIT")
        threading.Thread(target=self._record_worker, args=(freq_hz,), daemon=True).start()

    def stop_record(self):
        if not self.is_recording:
            return
        self.is_recording = False
        self.rec_btn.config(text="● REC", bg="#18181b", fg="#ef4444")
        self.play_btn.config(state="normal")
        self.status_badge.config(text="ONLINE", fg="#4ade80", bg="#14291d")
        with self.ser_lock:
            try:
                if self.ser and self.ser.is_open:
                    self.ser.write(b"X")
                    time.sleep(0.05)
            except Exception:
                pass
        if self.rec_freq_hz < 2_400_000_000:
            cleaned = [p for p in self.rec_pulses if 80 <= abs(p) <= 35000]
            if len(cleaned) >= 12:
                self.rec_count += 1
                item = CaptureItem(
                    name=f"REC_{self.rec_count:02d}_{self.rec_freq_hz/1e6:.2f}MHz.sub",
                    freq_hz=self.rec_freq_hz, preset=self.rec_preset,
                    protocol="RAW", raw_data=cleaned, radio_id="CC1101",
                    decode_stage="RAW_PULSES")
                self.add_capture(item)
                decode = item.rc_info["protocol"] if item.rc_info else "RAW"
                self.log(f"[+] Saved {len(cleaned)} pulses ({decode})")
            else:
                self.log(f"[!] Discarded: only {len(cleaned)} pulses")

    def _record_worker(self, freq_hz):
        freq_word = int((freq_hz / 26000000.0) * 65536)
        f2 = (freq_word >> 16) & 0xFF
        f1 = (freq_word >> 8) & 0xFF
        f0 = freq_word & 0xFF
        ch = max(0, min(125, int(freq_hz / 1e6 - 2400)))
        unit_idx = self.nrf_unit_combo.current()
        try:
            with self.ser_lock:
                if not self.ser or not self.ser.is_open:
                    return
                if freq_hz >= 2_400_000_000:
                    self.ser.write(bytes([ord("U"), unit_idx]))
                    self.ser.read(1)
                    self.ser.write(bytes([ord("M"), ch]))
                    self.ser.read(1)
                else:
                    p = self.preset_to_byte(self.rec_preset)
                    self.ser.write(bytes([ord("C"), p])); self.ser.read(1)
                    self.ser.write(bytes([ord("F"), f2, f1, f0])); self.ser.read(1)
                    self.ser.write(b"L"); self.ser.read(1)
            last = time.time()
            while self.is_recording:
                with self.ser_lock:
                    if not self.ser or not self.ser.is_open:
                        break
                    line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if line.startswith("NRF_RX:") and freq_hz >= 2_400_000_000:
                    hex_pkt = line.split(":", 1)[1].strip()
                    now = time.time()
                    unit = self.nrf_unit_combo.get()
                    self.root.after(0, lambda c=ch, u=unit: self.visualizer.push_waterfall_row(
                        [3 if i == c else 0 for i in range(126)],
                        f"{u} PKT CH{c}", stage="PACKET RX"))
                    if hex_pkt != self.last_nrf_hex or (now - self.last_nrf_ts) > 1.0:
                        self.last_nrf_hex = hex_pkt
                        self.last_nrf_ts = now
                        item = CaptureItem(
                            name=f"{unit}_PKT_{len(self.queue)+1:02d}",
                            freq_hz=freq_hz, protocol="NRF24", hex_data=hex_pkt,
                            radio_id=unit, decode_stage="PACKET_RX")
                        self.root.after(0, lambda it=item: self.add_capture(it))
                        self.root.after(0, lambda h=hex_pkt: self.log(f"[PKT] {h[:32]}"))
                elif freq_hz < 2_400_000_000:
                    nums = [int(n) for n in re.findall(r"-?\d+", line)]
                    for n in nums:
                        if 40 <= abs(n) <= 50000:
                            self.rec_pulses.append(n)
                    if time.time() - last >= 0.4 and self.rec_pulses:
                        snap = list(self.rec_pulses[-600:])
                        self.root.after(0, lambda s=snap: self.visualizer.set_waveform(
                            s, f"LIVE {len(s)} pulses", stage="EDGE STREAM"))
                        last = time.time()
        except Exception as e:
            self.root.after(0, lambda err=e: self.log(f"[!] RX error: {err}"))
        finally:
            self.is_recording = False
            self.root.after(0, lambda: self.rec_btn.config(text="● REC",
                                                            bg="#18181b", fg="#ef4444"))
            self.root.after(0, lambda: self.play_btn.config(state="normal"))

    def clear_live_rx(self):
        self.rec_pulses.clear()
        self.visualizer.set_waveform([], "CLEARED", stage="IDLE")

    @staticmethod
    def preset_to_byte(preset_str):
        if "Ook270" in preset_str: return 1
        if "238" in preset_str: return 2
        if "476" in preset_str: return 3
        return 0

    # ---------------- Sub-GHz sweep ----------------
    def start_sweep(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Offline", "Connect first.")
            return
        if self.is_recording or self.is_playing:
            return
        self.is_sweeping = True
        self.sweep_hits = []
        self.log("[*] Sub-GHz sweep started (RX only): " + ", ".join(f"{f} MHz" for f in SWEEP_FREQS_MHZ))
        threading.Thread(target=self._sweep_worker, daemon=True).start()

    def _sweep_worker(self):
        p_byte = self.preset_to_byte(PRESET_MAP["AM650 (OOK 650kHz)"])
        for mhz in SWEEP_FREQS_MHZ:
            if not self.is_sweeping or not self.ser:
                break
            freq_hz = int(mhz * 1_000_000)
            freq_word = int((freq_hz / 26000000.0) * 65536)
            f2, f1, f0 = (freq_word >> 16) & 0xFF, (freq_word >> 8) & 0xFF, freq_word & 0xFF
            try:
                with self.ser_lock:
                    if not self.ser or not self.ser.is_open:
                        break
                    self.ser.write(bytes([ord("C"), p_byte])); self.ser.read(1)
                    self.ser.write(bytes([ord("F"), f2, f1, f0])); self.ser.read(1)
                    self.ser.write(b"L"); self.ser.read(1)
                t0 = time.time()
                pulses = []
                while time.time() - t0 < 0.4:
                    with self.ser_lock:
                        if not self.ser or not self.ser.is_open:
                            break
                        line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                    if line:
                        nums = [int(n) for n in re.findall(r"-?\d+", line)]
                        pulses += [n for n in nums if 40 <= abs(n) <= 50000]
                    else:
                        time.sleep(0.01)
                self.root.after(0, lambda m=mhz, p=len(pulses):
                                self.log(f"  {m:.2f} MHz: {p} pulses"))
                if len(pulses) >= 12:
                    self.sweep_hits.append((mhz, len(pulses)))
            except Exception as e:
                self.root.after(0, lambda err=e: self.log(f"[!] Sweep {mhz}: {err}"))
        self.is_sweeping = False
        if self.sweep_hits:
            self.root.after(0, lambda: self.log(
                f"[+] Sweep found {len(self.sweep_hits)} active freq(s): " +
                ", ".join(f"{m} MHz" for m, _ in self.sweep_hits)))
        else:
            self.root.after(0, lambda: self.log("[*] Sweep complete — no active frequencies."))

    # ---------------- NRF occupancy scan ----------------
    def toggle_nrf_scan(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Offline", "Connect first.")
            return
        if self.is_recording or self.is_playing:
            return
        self.is_nrf_scanning = not self.is_nrf_scanning
        if self.is_nrf_scanning:
            self.nrf_scan_btn.config(text="■ STOP", bg="#7e22ce", fg="white")
            self.log("[*] NRF occupancy scan started (RX only).")
            threading.Thread(target=self._nrf_scan_worker, daemon=True).start()
        else:
            self.nrf_scan_btn.config(text="OCCUPANCY SCAN", bg="#18181b", fg="#38bdf8")
            self.log("[*] Scan stopped.")

    def snapshot_scan(self):
        if not self.ser or not self.ser.is_open:
            return
        threading.Thread(target=self._single_nrf_scan, daemon=True).start()

    def _nrf_scan_worker(self):
        while self.is_nrf_scanning:
            self._single_nrf_scan()
            time.sleep(0.35)

    def _single_nrf_scan(self):
        if self.is_recording or self.is_playing:
            return
        unit = self.nrf_unit_combo.get()
        idx = self.nrf_unit_combo.current()
        try:
            with self.ser_lock:
                if not self.ser or not self.ser.is_open:
                    return
                self.ser.reset_input_buffer()
                self.ser.write(bytes([ord("U"), idx]))
                self.ser.read(1)
                self.ser.write(b"S")
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if line.startswith("NRF_SCAN"):
                bits = line.split(":", 1)[1].ljust(126, "0")[:126]
                levels = [3 if b == "1" else 0 for b in bits]
                hits = [i for i, b in enumerate(bits) if b == "1"]
                self.root.after(0, lambda l=levels, u=unit, h=len(hits):
                                self.visualizer.push_waterfall_row(
                                    l, f"{u} OCCUPANCY", stage=f"{h} CH ACTIVE"))
                if hits and not self.is_nrf_scanning:
                    self.root.after(0, lambda h=hits: self.log(
                        "  Active channels: " + ", ".join(
                            f"CH{c}({2400+c}MHz)" for c in h[:12])))
            else:
                self.root.after(0, lambda l=line: self.log(f"[!] Scan: {l!r}"))
        except Exception as e:
            self.root.after(0, lambda err=e: self.log(f"[!] Scan: {err}"))

    # ---------------- capture bank ----------------
    def _selected_index(self):
        s = self.tree.selection()
        return self.tree.index(s[0]) if s else None

    def _refresh_tree(self, select_idx=None):
        self.tree.delete(*self.tree.get_children())
        for item in self.queue:
            tag = "NRF" if item.is_nrf24 else "SUBG"
            self.tree.insert("", tk.END,
                             values=(item.band_label, item.name, item.freq_label,
                                     item.protocol, item.detail_label),
                             tags=(tag,))
        if select_idx is not None:
            children = self.tree.get_children()
            if 0 <= select_idx < len(children):
                self.tree.selection_set(children[select_idx])
                self.tree.see(children[select_idx])

    def on_select_capture(self, _evt=None):
        idx = self._selected_index()
        if idx is None:
            return
        item = self.queue[idx]
        self.freq_combo.set(f"{item.freq_hz/1e6:.2f}")
        if item.preset in REV_PRESET_MAP:
            self.preset_combo.set(REV_PRESET_MAP[item.preset])
        if item.is_nrf24:
            ch = max(0, min(125, int(item.freq_hz/1e6 - 2400)))
            self.visualizer.push_waterfall_row(
                [2 if i == ch else 0 for i in range(126)],
                f"SELECTED {item.name}", stage=item.decode_stage)
        else:
            self.visualizer.set_waveform(item.raw_data,
                f"{item.name}", push_density=True, stage=item.decode_stage)

    def add_capture(self, item):
        self.queue.append(item)
        self._refresh_tree(select_idx=len(self.queue) - 1)
        self.on_select_capture()
        self.save_bank()

    def clear_queue(self):
        self.queue.clear()
        self.tree.delete(*self.tree.get_children())
        self.save_bank()
        self.log("[*] Bank cleared.")

    def delete_selected(self):
        idx = self._selected_index()
        if idx is None:
            return
        removed = self.queue.pop(idx)
        nxt = min(idx, len(self.queue) - 1) if self.queue else None
        self._refresh_tree(select_idx=nxt)
        self.save_bank()
        self.log(f"[-] Removed: {removed.name}")

    def rename_selected(self):
        idx = self._selected_index()
        if idx is None:
            return
        item = self.queue[idx]
        new = simpledialog.askstring("Rename", "New name:",
                                     initialvalue=item.name, parent=self.root)
        if new and new.strip():
            item.name = new.strip()
            self._refresh_tree(select_idx=idx)
            self.save_bank()

    def save_selected_capture(self):
        idx = self._selected_index()
        if idx is None:
            messagebox.showwarning("Nothing Selected", "Select a capture.")
            return
        item = self.queue[idx]
        if item.raw_data:
            p = filedialog.asksaveasfilename(
                defaultextension=".sub",
                initialfile=os.path.splitext(item.name)[0] + ".sub",
                filetypes=[("Flipper Sub", "*.sub")])
            if p:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(item.to_flipper_sub())
                self.log(f"[+] Exported .sub -> {p}")
        elif item.hex_data:
            p = filedialog.asksaveasfilename(
                defaultextension=".md",
                initialfile=os.path.splitext(item.name)[0] + ".md",
                filetypes=[("Markdown", "*.md")])
            if p:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(item.to_markdown_block())
                self.log(f"[+] Exported .md -> {p}")

    def handle_drop(self, ev):
        for path in self.root.tk.splitlist(ev.data):
            self.ingest_file(path)

    def browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Select .sub / .raw / .md",
            filetypes=[("RF / MD", "*.sub *.raw *.md *.txt"), ("All", "*.*")])
        for p in paths:
            self.ingest_file(p)

    def ingest_file(self, path):
        if not os.path.isfile(path):
            return
        ext = os.path.splitext(path)[1].lower()
        base = os.path.basename(path)
        try:
            if ext == ".raw":
                item = self.parse_raw_file(path, base)
                if item:
                    self.add_capture(item)
                    self.log(f"[+] Loaded .raw: {item.summary()}")
                return
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if ext == ".sub":
                item = self.parse_sub_block(content, base, path)
                if item:
                    self.add_capture(item)
                    self.log(f"[+] Loaded .sub: {item.summary()}")
            else:
                items = self.parse_markdown(content, base, path)
                for it in items:
                    self.add_capture(it)
                if items:
                    self.log(f"[+] Extracted {len(items)} from {base}")
        except Exception as e:
            self.log(f"[!] Error: {base} — {e}")

    def parse_raw_file(self, path, name):
        try:
            fhz = int(float(self.freq_combo.get()) * 1_000_000)
        except ValueError:
            fhz = 433920000
        preset = PRESET_MAP.get(self.preset_combo.get(), "FuriHalSubGhzPresetOok650Async")
        us = int(self.raw_rate_combo.get())
        with open(path, "rb") as f:
            raw = f.read()
        if not raw:
            return None
        if b"\x00" not in raw[:512]:
            text = raw.decode("utf-8", errors="ignore").strip()
            nums = [int(n) for n in re.findall(r"-?\d+", text)]
            pulses = [n for n in nums if 20 <= abs(n) <= 200000]
            if len(pulses) >= 4:
                if all(p > 0 for p in pulses):
                    pulses = [p if i % 2 == 0 else -p for i, p in enumerate(pulses)]
                return CaptureItem(name=name, source_file=path, freq_hz=fhz,
                                   preset=preset, protocol="RAW-TXT",
                                   raw_data=pulses, radio_id="CC1101")
        if len(raw) >= 16:
            mid = (min(raw) + max(raw)) // 2
            if mid > 0:
                pulses = []
                cur = raw[0] > mid
                run = 0
                for b in raw[:120000]:
                    hi = b > mid
                    if hi == cur:
                        run += us
                    else:
                        if run >= 40:
                            pulses.append(run if cur else -run)
                        cur = hi
                        run = us
                if run >= 40:
                    pulses.append(run if cur else -run)
                if len(pulses) >= 4:
                    return CaptureItem(name=name, source_file=path, freq_hz=fhz,
                                       preset=preset, protocol=f"RAW-U8@{us}u",
                                       raw_data=pulses, radio_id="CC1101")
        return None

    def parse_sub_block(self, text, name, path):
        meta, raw = {}, []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "RAW_Data":
                raw.extend(int(x) for x in re.findall(r"-?\d+", v))
            else:
                meta[k] = v
        if not raw and not meta.get("Data"):
            self.log(f"[!] {name}: no RAW_Data or Data field found")
            return None
        fhz = int(meta.get("Frequency", 433920000))
        return CaptureItem(name=name, source_file=path, freq_hz=fhz,
                           preset=meta.get("Preset", "FuriHalSubGhzPresetOok650Async"),
                           protocol=meta.get("Protocol", "RAW"),
                           raw_data=raw, hex_data=meta.get("Data", ""),
                           radio_id="CC1101" if fhz < 2_400_000_000 else self.nrf_unit_combo.get())

    def parse_markdown(self, text, name, path):
        out = []
        for idx, sec in enumerate(re.split(r"(?m)^#{1,4}\s+", text)):
            lines = sec.strip().splitlines()
            if not lines:
                continue
            title = lines[0].strip("#*` ") if idx > 0 else name
            item = self.parse_sub_block(sec, title or name, path)
            if item:
                out.append(item)
        return out

    # ---------------- quick .sub loader ----------------
    def _quick_load_sub(self):
        paths = filedialog.askopenfilenames(
            title="Load .sub file(s)",
            filetypes=[("Flipper Sub", "*.sub"),
                       ("RAW", "*.raw"),
                       ("All", "*.*")])
        if not paths:
            return
        before = len(self.queue)
        for path in paths:
            self.ingest_file(path)
        added = len(self.queue) - before
        if added > 0:
            self._refresh_tree(select_idx=len(self.queue) - 1)
            self.on_select_capture()
            self.log(f"[+] Loaded {added} capture(s); newest selected - press PLAY")
        else:
            self.log(f"[!] Nothing new loaded (parse rejected {len(paths)} file(s))")

    # ---------------- play / TX ----------------
    def trigger_play(self):
        if self.is_playing or self.is_recording:
            return
        idx = self._selected_index()
        if idx is None:
            if not self.queue:
                messagebox.showinfo(
                    "No captures loaded",
                    "Load a .sub file first (LOAD .SUB button or drop onto the tree).")
            else:
                messagebox.showinfo(
                    "No capture selected",
                    "Click a row in the capture bank to select it, then press PLAY.")
            return
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Offline", "Connect to the ESP32 first.")
            return
        item = self.queue[idx]
        if not item.raw_data and not item.hex_data:
            messagebox.showerror("Empty capture",
                                 f"{item.name} has no raw_data or hex_data.")
            return
        self.is_playing = True
        self.play_btn.config(text="■ TX...", bg="#991b1b", state="disabled")
        self.rec_btn.config(state="disabled")
        threading.Thread(target=self._play_worker, args=(item,), daemon=True).start()

    def _play_worker(self, item):
        repeats = int(self.repeat_spin.get())
        freq_word = int((item.freq_hz / 26000000.0) * 65536)
        f2, f1, f0 = (freq_word >> 16) & 0xFF, (freq_word >> 8) & 0xFF, freq_word & 0xFF
        try:
            for r in range(1, repeats + 1):
                self.root.after(0, lambda i=r: self.log(
                    f"[>] TX [{i}/{repeats}] {item.name} @ {item.freq_hz/1e6:.2f} MHz"))
                if item.is_nrf24:
                    clean = re.sub(r"[^0-9A-Fa-f]", "", item.hex_data)
                    if not clean:
                        break
                    raw = bytes.fromhex(clean)[:32]
                    ch = max(0, min(125, int(item.freq_hz/1e6 - 2400)))
                    with self.ser_lock:
                        self.ser.write(bytes([ord("N"), ch, 1, len(raw)]) + raw)
                        self.ser.read(1)
                else:
                    if not item.raw_data:
                        break
                    packed = []
                    for val in item.raw_data:
                        s = 1 if val >= 0 else -1
                        rem = abs(val)
                        while rem > 0:
                            step = min(rem, 32767)
                            packed.append(s * step)
                            rem -= step
                    packed = packed[:65535]
                    hdr = struct.pack(">cH", b"P", len(packed))
                    payload = struct.pack(f">{len(packed)}h", *packed)
                    p = self.preset_to_byte(item.preset)
                    with self.ser_lock:
                        self.ser.write(bytes([ord("C"), p])); self.ser.read(1)
                        self.ser.write(bytes([ord("F"), f2, f1, f0])); self.ser.read(1)
                        self.ser.write(hdr + payload); self.ser.read(1)
                if r < repeats:
                    time.sleep(0.15)
            self.root.after(0, lambda: self.log("[+] TX complete."))
        except Exception as e:
            self.root.after(0, lambda err=e: self.log(f"[!] TX error: {err}"))
        finally:
            self.is_playing = False
            self.root.after(0, lambda: self.play_btn.config(
                text="▶ PLAY .SUB", bg="#dc2626", state="normal"))
            self.root.after(0, lambda: self.rec_btn.config(state="normal"))

    # ---------------- JitterFood ----------------
    def launch_icecream(self):
        """Open the ice-cream module window, sized to fit its content."""
        import sys, subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "IceCream.py")
        if not os.path.isfile(script):
            messagebox.showerror("Missing",
                "IceCream.py not found next to JitterDeck4.py")
            return
        try:
            subprocess.Popen([sys.executable, script], cwd=here,
                             start_new_session=True)
            self.log("[*] Launched ICE CREAM module")
        except Exception as e:
            messagebox.showerror("Launch failed", str(e))

    def launch_jitterfood(self):
        import sys, subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "JitterFood.py")
        if not os.path.isfile(script):
            messagebox.showerror("Missing", "JitterFood.py not found next to JitterDeck3.py")
            return
        try:
            subprocess.Popen([sys.executable, script], cwd=here, start_new_session=True)
            self.log(f"[*] Launched JitterFood")
        except Exception as e:
            messagebox.showerror("Launch failed", str(e))

    def on_close(self):
        self.is_recording = False
        self.is_sweeping = False
        self.is_nrf_scanning = False
        self.save_bank()
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(b"X")
                    self.ser.close()
                except Exception:
                    pass
        self.root.destroy()


if __name__ == "__main__":
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    app = JitterDeck4(root)
    root.mainloop()


# === JITTERDECK4 PATCH BLOCK: SUB-GHZ BAR + RFCLOWN + SWEEP FIX + PORT LOCK ===
import json as _jd_json
import os as _jd_os
import subprocess as _jd_subp
import sys as _jd_sys
import time as _jd_time
import tkinter as _jd_tk
from tkinter import ttk as _jd_ttk

_JD_PORT_LOCK_FILE = _jd_os.path.expanduser("~/.jitter_port.lock")


def jd_write_port_lock(port_name):
    try:
        payload = {"pid": _jd_os.getpid(), "port": port_name, "ts": _jd_time.time()}
        with open(_JD_PORT_LOCK_FILE, "w", encoding="utf-8") as f:
            _jd_json.dump(payload, f)
    except OSError:
        pass


def jd_clear_port_lock():
    try:
        if _jd_os.path.exists(_JD_PORT_LOCK_FILE):
            with open(_JD_PORT_LOCK_FILE, "r", encoding="utf-8") as f:
                data = _jd_json.load(f)
            if data.get("pid") == _jd_os.getpid():
                _jd_os.remove(_JD_PORT_LOCK_FILE)
    except OSError:
        pass


class SubGhzRfClownBar(_jd_ttk.LabelFrame):
    """Single non-overlapping Sub-GHz + RFclown emulator control bar."""

    def __init__(self, parent, deck):
        super().__init__(parent, text="SUB-GHZ & RFCLOWN EMULATOR", padding=6)
        self.deck = deck
        self._build()

    def _send(self, cmd: str):
        if hasattr(self.deck, "send_line"):
            self.deck.send_line(cmd)
        elif getattr(self.deck, "ser", None) and self.deck.ser.is_open:
            self.deck.ser.write((cmd.strip() + "\n").encode("utf-8"))

    def _build(self):
        row0 = _jd_ttk.Frame(self)
        row0.pack(fill="x", pady=(0, 4))

        _jd_ttk.Label(row0, text="Freq (MHz):").pack(side="left", padx=(0, 3))
        self.freq_var = _jd_tk.StringVar(value="433.92")
        self.freq_combo = _jd_ttk.Combobox(
            row0,
            textvariable=self.freq_var,
            values=["300.00", "315.00", "390.00", "433.92", "868.35", "915.00"],
            width=8,
        )
        self.freq_combo.pack(side="left", padx=(0, 6))
        self.freq_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_freq())

        _jd_ttk.Label(row0, text="Mod:").pack(side="left", padx=(0, 3))
        self.mod_var = _jd_tk.StringVar(value="OOK")
        self.mod_combo = _jd_ttk.Combobox(
            row0,
            textvariable=self.mod_var,
            values=["OOK", "FSK", "GFSK"],
            state="readonly",
            width=6,
        )
        self.mod_combo.pack(side="left", padx=(0, 8))
        self.mod_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_mod())

        if hasattr(self.deck, "toggle_record"):
            _jd_ttk.Button(row0, text="● REC", command=self.deck.toggle_record).pack(side="left", padx=2)
        if hasattr(self.deck, "stop_all"):
            _jd_ttk.Button(row0, text="■ STOP", command=self.deck.stop_all).pack(side="left", padx=2)
        if hasattr(self.deck, "play_sub"):
            _jd_ttk.Button(row0, text="▶ PLAY .SUB", command=self.deck.play_sub).pack(side="left", padx=2)
        if hasattr(self.deck, "toggle_sweep"):
            _jd_ttk.Button(row0, text="⟳ SWEEP", command=self.deck.toggle_sweep).pack(side="left", padx=2)

        row1 = _jd_ttk.Frame(self)
        row1.pack(fill="x")

        _jd_ttk.Label(row1, text="RFclown Bank:").pack(side="left", padx=(0, 3))
        self.bank_var = _jd_tk.StringVar(value="0")
        self.bank_combo = _jd_ttk.Combobox(
            row1,
            textvariable=self.bank_var,
            values=["0", "1", "2", "3", "4", "5", "6", "7"],
            state="readonly",
            width=3,
        )
        self.bank_combo.pack(side="left", padx=(0, 6))
        self.bank_combo.bind("<<ComboboxSelected>>", lambda _e: self.set_bank())

        _jd_ttk.Button(row1, text="READ SLOT", command=self.rfc_read).pack(side="left", padx=2)
        _jd_ttk.Button(row1, text="CLONE TX", command=self.rfc_clone).pack(side="left", padx=2)

        self.emul_active = False
        self.emul_btn = _jd_ttk.Button(row1, text="EMULATE OFF", command=self.rfc_toggle_emulate)
        self.emul_btn.pack(side="left", padx=2)

        _jd_ttk.Button(row1, text="STATUS", command=self.rfc_status).pack(side="left", padx=2)
        self.rfc_status_lbl = _jd_ttk.Label(row1, text="RFclown: IDLE")
        self.rfc_status_lbl.pack(side="left", padx=(8, 0))

    def apply_freq(self):
        val = self.freq_var.get().strip()
        self._send(f"C:FREQ:{val}")
        self.rfc_status_lbl.config(text=f"RFclown: {val} MHz")

    def apply_mod(self):
        mod = self.mod_var.get().strip()
        self._send(f"C:MOD:{mod}")
        self.rfc_status_lbl.config(text=f"RFclown: MOD={mod}")

    def set_bank(self):
        bank = self.bank_var.get().strip()
        self._send(f"C:BANK:{bank}")
        self.rfc_status_lbl.config(text=f"RFclown: BANK {bank}")

    def rfc_read(self):
        self.set_bank()
        self._send("C:READ")
        self.rfc_status_lbl.config(text=f"RFclown: READ -> BANK {self.bank_var.get()}")

    def rfc_clone(self):
        bank = self.bank_var.get().strip()
        self._send(f"C:CLONE:{bank}")
        self.rfc_status_lbl.config(text=f"RFclown: CLONE TX BANK {bank}")

    def rfc_toggle_emulate(self):
        bank = self.bank_var.get().strip()
        if not self.emul_active:
            self.emul_active = True
            self.emul_btn.config(text="EMULATE ON")
            self._send(f"C:EMUL:{bank}")
            self.rfc_status_lbl.config(text=f"RFclown: EMULATING BANK {bank}")
        else:
            self.emul_active = False
            self.emul_btn.config(text="EMULATE OFF")
            self._send("C:EMUL:0")
            self.rfc_status_lbl.config(text="RFclown: EMUL STOPPED")

    def rfc_status(self):
        self._send("C:STATUS")
        self.rfc_status_lbl.config(text="RFclown: QUERYING...")


def jd_launch_helper_releasing_port(deck, script_name: str):
    try:
        if getattr(deck, "ser", None) and deck.ser.is_open:
            deck.ser.close()
    except Exception:
        pass
    deck.ser = None
    jd_clear_port_lock()
    script_path = _jd_os.path.join(_jd_os.path.dirname(_jd_os.path.abspath(__file__)), script_name)
    _jd_subp.Popen([_jd_sys.executable, script_path])
