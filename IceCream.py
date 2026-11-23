#!/usr/bin/env python3
"""
IceCream — 2.4 GHz command console for JitterDeck (JitterBox6 firmware).

Protocol recap (all 'K'/'D'/'E' terminated):
  U <idx>            select active NRF (0=NRF1, 1=NRF2, 2=NRF3)      -> 'K'
  M <ch>             start RX listen on channel 0-125                -> 'K'
  S                  RPD occupancy scan (returns NRF_SCAN:<126bits>) -> text
  N <ch> <rate> <len> + payload bytes   single TX packet              -> 'D'/'E'
  T <c0> <c1> <c2>   tri-listen on 3 channels (BLE 2/26/80 etc.)     -> 'K'
  X                  stop all listening                              -> 'K'
  V                  version banner (returns text line)
"""

import os
import re
import sys
import time
import json
import glob
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import serial

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from JitterDeck4 import parse_ir_file, export_ir_file
except Exception:
    def parse_ir_file(path):
        sigs, cur = [], None
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, v = [x.strip() for x in line.split(":", 1)]
                if k == "name":
                    if cur: sigs.append(cur)
                    cur = {"name": v, "type": "raw", "frequency": 38000,
                           "duty_cycle": 0.33, "data": []}
                elif cur is not None:
                    if k == "type": cur["type"] = v.lower()
                    elif k == "frequency":
                        try: cur["frequency"] = int(float(v))
                        except ValueError: pass
                    elif k == "duty cycle":
                        try: cur["duty_cycle"] = float(v)
                        except ValueError: pass
                    elif k == "data":
                        cur["data"] = [int(t) for t in v.replace(",", " ").split()
                                       if t.lstrip("-").isdigit()]
        if cur: sigs.append(cur)
        return sigs
    def export_ir_file(path, sigs):
        lines = ["Filetype: IR signals file", "Version: 1"]
        for i, sig in enumerate(sigs, 1):
            lines += [f"# Signal {i}", f"name: {sig.get('name', f'SIG_{i}')}",
                      f"type: {sig.get('type', 'raw')}"]
            if sig.get("type", "raw") == "raw":
                lines += [f"frequency: {int(sig.get('frequency', 38000))}",
                          f"duty cycle: {sig.get('duty_cycle', 0.33)}",
                          "data: " + " ".join(str(int(x)) for x in sig.get("data", []))]
            lines.append("")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).strip() + "\n")

# --- BLE advertising channels the 3 NRFs are typically pinned to ----------
BLE_TRIPLE = (2, 26, 80)
DEFAULT_BAUD = 115200

# NRF channel <-> center frequency (2400 MHz + ch)
def ch_to_mhz(ch): return 2400 + int(ch)
def mhz_to_ch(mhz): return max(0, min(125, int(mhz) - 2400))

RATES = {"1 Mbps": 0, "2 Mbps": 1, "250 kbps": 2}
REV_RATES = {v: k for k, v in RATES.items()}


# ---------------------------------------------------------------------------
# Serial bridge
# ---------------------------------------------------------------------------
class RadioLink:
    """Thread-safe serial wrapper for the JitterBox6 command protocol."""

    def __init__(self, port=None, baud=DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self.ser = None
        self.lock = threading.Lock()

    def open(self, port=None, baud=None):
        if self.ser and self.ser.is_open:
            self.close()
        self.port = port or self.port
        self.baud = baud or self.baud
        if not self.port:
            return False, "no port"
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.5)
            self.ser.dtr = False
            self.ser.rts = False
            time.sleep(0.2)
            self.ser.reset_input_buffer()
            return True, "ok"
        except Exception as e:
            self.ser = None
            return False, str(e)

    def close(self):
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(b"X")
                except Exception:
                    pass
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None

    @property
    def is_open(self):
        return bool(self.ser and self.ser.is_open)

    def _cmd(self, byte_cmds, ack_len=1, timeout=0.6):
        """Send raw bytes, read ack_len bytes, return ack (bytes) or b''."""
        if not self.is_open:
            return b""
        with self.lock:
            try:
                self.ser.reset_input_buffer()
                self.ser.write(byte_cmds)
                self.ser.timeout = timeout
                return self.ser.read(ack_len)
            except Exception:
                return b""

    def send_u(self, idx):
        return self._cmd(bytes([ord("U"), idx & 0x03])) == b"K"

    def send_m(self, ch):
        return self._cmd(bytes([ord("M"), ch & 0x7F])) == b"K"

    def send_t(self, c0, c1, c2):
        return self._cmd(bytes([ord("T"), c0 & 0x7F, c1 & 0x7F, c2 & 0x7F])) == b"K"

    def send_stop(self):
        return self._cmd(b"X") == b"K"

    def send_carrier(self, ch, radio):
        # Opcode 'K' — 'J' collides with JitterGotchi button subcommands
        return self._cmd(bytes([ord("K"), ch & 0x7F, radio & 0x03]))

    def send_carrier_stop(self):
        return self._cmd(bytes([ord("K"), 0xFF]))

    def send_scan(self):
        """RPD occupancy scan — returns 126-char string of '0'/'1' or ''."""
        if not self.is_open:
            return ""
        with self.lock:
            try:
                self.ser.reset_input_buffer()
                self.ser.write(b"S")
                self.ser.timeout = 3.0
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if line.startswith("NRF_SCAN:"):
                    return line.split(":", 1)[1]
            except Exception:
                pass
        return ""

    def send_n(self, ch, rate, payload):
        """Single TX packet. rate=0/1/2, payload up to 32 bytes."""
        if not self.is_open:
            return b""
        payload = (payload or b"\x00" * 32)[:32]
        payload = payload + b"\x00" * (32 - len(payload))
        with self.lock:
            try:
                self.ser.reset_input_buffer()
                self.ser.write(bytes([ord("N"), ch & 0x7F, rate & 0x03, len(payload)]))
                self.ser.write(payload)
                self.ser.timeout = 1.0
                return self.ser.read(1)
            except Exception:
                return b""

    def send_ir_json(self, obj, timeout=1.5):
        if not self.is_open:
            return ""
        line = (json.dumps(obj) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.ser.reset_input_buffer()
                self.ser.write(line)
                self.ser.timeout = timeout
                return self.ser.readline().decode("utf-8", errors="ignore").strip()
            except Exception:
                return ""

    def send_ir_mode(self, to_ir):
        return self._cmd(bytes([ord("G"), 1 if to_ir else 0]))

    def send_ir_recv_burst(self, timeout=2.0):
        if not self.is_open:
            return []
        with self.lock:
            try:
                self.ser.timeout = timeout
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    return []
                return [int(t) for t in line.replace(",", " ").split()
                        if t.lstrip("-").isdigit()]
            except Exception:
                return []

    def read_packet(self, timeout=0.1):
        """Poll for a NRF_RX:... line. Returns hex string or ''."""
        if not self.is_open:
            return ""
        with self.lock:
            try:
                self.ser.timeout = timeout
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if line.startswith("NRF_RX"):
                    if ":" in line:
                        return line.split(":", 1)[1]
                    return line
            except Exception:
                pass
        return ""


# ---------------------------------------------------------------------------
# Little "GameBoy" OLED emulator — mirrors the firmware's menu state
# ---------------------------------------------------------------------------
class GameboyOLED(tk.Canvas):
    """
    128x64 monochrome emulation of the 0.96" OLED on the shield.
    Renders the same menu list the firmware shows: WiFi / Video TX / RC /
    BLE / Bluetooth / USB Wireless / Zigbee / NRF24. Cursor scrolls with
    the ◀ ▶ buttons on the IceCream window or the arrow keys.
    """

    MODES = ["WiFi", "Video TX", "RC", "BLE",
             "Bluetooth", "USB Wireless", "Zigbee", "NRF24"]
    PW, PH = 128, 64          # logical OLED pixels
    SCALE = 3                 # 384x192 on screen
    HEADER_H = 10

    def __init__(self, parent, **kw):
        super().__init__(parent, width=self.PW * self.SCALE,
                         height=self.PH * self.SCALE,
                         bg="#0a0a0a", highlightthickness=0, **kw)
        self.cursor = 0
        self.active = False
        self._dirty = True
        self.after(50, self._tick)

    # ---- external API ----------------------------------------------------
    def set_cursor(self, idx):
        self.cursor = max(0, min(len(self.MODES) - 1, idx))
        self._dirty = True

    def prev(self):
        self.set_cursor((self.cursor - 1) % len(self.MODES))

    def next(self):
        self.set_cursor((self.cursor + 1) % len(self.MODES))

    def toggle_active(self):
        self.active = not self.active
        self._dirty = True

    @property
    def current_mode(self):
        return self.MODES[self.cursor]

    # ---- rendering -------------------------------------------------------
    def _tick(self):
        if self._dirty:
            self._dirty = False
            self._redraw()
        self.after(80, self._tick)

    def _px(self, x, y, w=1, h=1, col="#7cf0c1"):
        s = self.SCALE
        self.create_rectangle(x * s, y * s, (x + w) * s, (y + h) * s,
                              fill=col, outline="")

    def _redraw(self):
        self.delete("all")
        s = self.SCALE
        # bezel
        self.create_rectangle(0, 0, self.PW * s, self.PH * s,
                              fill="#0e2419", outline="#1f3a2a", width=2)
        # header bar (inverted, like drawHeaderBar() in firmware)
        for x in range(self.PW):
            for y in range(self.HEADER_H):
                self._px(x, y, col="#7cf0c1")
        # header text (dark pixels on bright bar) — crude 5x7 font
        self._text(6, 2, "RF-Clown", col="#0e2419")
        self._text(self.PW - 42, 2, "v2.0.0", col="#0e2419")

        # card body
        y0 = self.HEADER_H + 4
        h = 36
        self.create_rectangle(6 * s, y0 * s, (self.PW - 6) * s, (y0 + h) * s,
                              outline="#7cf0c1", width=2)
        label = self.MODES[self.cursor]
        self._text(12, y0 + 5, label, scale=2, col="#7cf0c1")
        status = "ACTIVE" if self.active else "DEACTIVE"
        col = "#7cf0c1" if self.active else "#3d6b52"
        self._text(12, y0 + 20, status, col=col)

        # pill toggle on the right
        px = self.PW - 34
        py = y0 + 12
        self.create_oval(px * s, py * s, (px + 14) * s, (py + 14) * s,
                         outline="#7cf0c1", width=2)
        self.create_oval((px + 14) * s, py * s, (px + 28) * s, (py + 14) * s,
                         outline="#7cf0c1", width=2)
        self.create_rectangle((px + 12) * s, (py + 2) * s,
                              (px + 16) * s, (py + 12) * s,
                              fill="#0e2419", outline="")
        knob_x = (px + 2) if self.active else (px + 16)
        self.create_oval(knob_x * s, (py + 2) * s,
                         (knob_x + 10) * s, (py + 12) * s,
                         fill="#7cf0c1", outline="")

        # pagination dots at y = 58 (firmware uses DOT_Y = 64-6)
        n = len(self.MODES)
        total_w = (n * 6) - 2
        start = (self.PW - total_w) // 2
        for i in range(n):
            cx = start + i * 6
            if i == self.cursor:
                self.create_oval(cx * s, 56 * s, (cx + 4) * s, 60 * s,
                                 fill="#7cf0c1", outline="")
            else:
                self.create_oval(cx * s, 56 * s, (cx + 4) * s, 60 * s,
                                 outline="#3d6b52", width=1)

    # ---- tiny 5x7-ish font so we don't depend on system fonts ----------
    _GLYPHS = {
        "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
        "B": ["11110", "10001", "11110", "10001", "10001", "10001", "11110"],
        "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
        "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
        "E": ["11111", "10000", "11110", "10000", "10000", "10000", "11111"],
        "F": ["11111", "10000", "11110", "10000", "10000", "10000", "10000"],
        "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
        "H": ["10001", "10001", "11111", "10001", "10001", "10001", "10001"],
        "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
        "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
        "M": ["10001", "11011", "10101", "10001", "10001", "10001", "10001"],
        "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
        "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
        "P": ["11110", "10001", "11110", "10000", "10000", "10000", "10000"],
        "R": ["11110", "10001", "11110", "10100", "10010", "10001", "10001"],
        "S": ["01111", "10000", "01110", "00001", "00001", "10001", "01110"],
        "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
        "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
        "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
        "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
        "X": ["10001", "01010", "00100", "00100", "00100", "01010", "10001"],
        "Z": ["11111", "00010", "00100", "00100", "01000", "10000", "11111"],
        "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
        "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
        "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
        "3": ["11110", "00001", "01110", "00001", "00001", "10001", "01110"],
        "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
        "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
        "6": ["01110", "10000", "11110", "10001", "10001", "10001", "01110"],
        "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
        "8": ["01110", "10001", "01110", "10001", "10001", "10001", "01110"],
        "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
        " ": ["00000"] * 7, ".": ["00000", "00000", "00000", "00000",
                                   "00000", "00110", "00110"],
        "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
        ":": ["00000", "00110", "00110", "00000", "00110", "00110", "00000"],
    }

    def _text(self, x, y, s, scale=1, col="#7cf0c1"):
        cx = x
        for ch in s.upper():
            g = self._GLYPHS.get(ch, self._GLYPHS[" "])
            for gy, row in enumerate(g):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        self._px(cx + gx * scale, y + gy * scale,
                                 w=scale, h=scale, col=col)
            cx += (5 + 1) * scale


# ---------------------------------------------------------------------------
# Main IceCream window
# ---------------------------------------------------------------------------
class IceCreamApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ICECREAM // 2.4 GHz Console")
        self.root.configure(bg="#09090b")

        self.link = RadioLink()
        self.selected_nrf = 0          # 0/1/2 -> NRF1/2/3
        self.listen_mode = None        # None | 'single' | 'triple'
        self.tx_rate = 1               # index into RATES
        self.rx_thread = None
        self.stop_rx = threading.Event()
        self.flood_thread = None
        self.stop_flood = threading.Event()
        self.flood_pkt_count = 0
        self.flood_t0 = 0.0
        self.carrier_active = {}
        self.ir_signals = []
        self.ir_mode = False
        self.ir_recording = False

        self._build_ui()
        self._autodetect_port()
        self._size_to_content()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ----------------------------------------------------------
    def _build_ui(self):
        # header
        hdr = tk.Frame(self.root, bg="#111115",
                       highlightbackground="#f472b6", highlightthickness=1)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(hdr, text="🍦 ICECREAM", fg="#f472b6", bg="#111115",
                 font=("Monospace", 12, "bold")).pack(side="left", padx=10, pady=6)
        self.status_lbl = tk.Label(hdr, text="OFFLINE", fg="#a1a1aa",
                                   bg="#1f1f26", font=("Monospace", 9, "bold"),
                                   padx=8, pady=2)
        self.status_lbl.pack(side="right", padx=10)

        # ---- link bar ----
        link = tk.Frame(self.root, bg="#09090b")
        link.pack(fill="x", padx=8, pady=2)
        tk.Label(link, text="PORT:", fg="#a1a1aa", bg="#09090b",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.port_combo = ttk.Combobox(link, width=16, state="readonly",
                                       font=("Monospace", 9))
        self.port_combo.pack(side="left", padx=4)
        tk.Button(link, text="↻", command=self._refresh_ports,
                  bg="#18181b", fg="#d4d4d8", relief="flat",
                  font=("Monospace", 10, "bold"), width=3
                  ).pack(side="left", padx=2)
        self.conn_btn = tk.Button(link, text="CONNECT", command=self._toggle_conn,
                                  bg="#dc2626", fg="white", relief="flat",
                                  font=("Monospace", 9, "bold"), padx=10, pady=2)
        self.conn_btn.pack(side="right")

        # ---- two-column body: OLED on left, controls on right ----
        body = tk.Frame(self.root, bg="#09090b")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        # LEFT: gameboy OLED emulator + nav buttons
        left = tk.Frame(body, bg="#09090b")
        left.pack(side="left", fill="y", padx=(0, 8))

        tk.Label(left, text="0.96\" OLED MIRROR", fg="#71717a", bg="#09090b",
                 font=("Monospace", 8, "bold")).pack(anchor="w")
        self.oled = GameboyOLED(left)
        self.oled.pack(pady=(2, 6))

        nav = tk.Frame(left, bg="#09090b")
        nav.pack()
        tk.Button(nav, text="◀", command=self._oled_prev,
                  bg="#18181b", fg="#e4e4e7", relief="flat",
                  font=("Monospace", 12, "bold"), width=4
                  ).pack(side="left", padx=2)
        tk.Button(nav, text="SELECT", command=self._oled_select,
                  bg="#14532d", fg="#4ade80", relief="flat",
                  font=("Monospace", 9, "bold"), padx=10
                  ).pack(side="left", padx=2)
        tk.Button(nav, text="▶", command=self._oled_next,
                  bg="#18181b", fg="#e4e4e7", relief="flat",
                  font=("Monospace", 12, "bold"), width=4
                  ).pack(side="left", padx=2)

        # RIGHT: notebook with 2.4 GHz and IR tabs
        nb = ttk.Notebook(body)
        nb.pack(side="left", fill="both", expand=True)
        right = tk.Frame(nb, bg="#09090b")
        nb.add(right, text="  2.4 GHz  ")
        ir_tab = tk.Frame(nb, bg="#09090b")
        nb.add(ir_tab, text="  IR  ")

        # --- NRF selector ---
        nrf_box = tk.LabelFrame(right, text=" RADIO ", bg="#111115", fg="#38bdf8",
                                font=("Monospace", 8, "bold"),
                                highlightbackground="#27272a", highlightthickness=1,
                                padx=8, pady=6)
        nrf_box.pack(fill="x", pady=2)
        self.nrf_var = tk.IntVar(value=0)
        for i, (lbl, pins) in enumerate([
            ("NRF1", "CSN 4 / CE 15"),
            ("NRF2", "CSN 48 / CE 47"),
            ("NRF3", "CSN 21 / CE 14 (IR shared)"),
        ]):
            tk.Radiobutton(nrf_box, text=f"{lbl}  ({pins})",
                           variable=self.nrf_var, value=i,
                           command=self._on_nrf_change,
                           bg="#111115", fg="#e4e4e7",
                           selectcolor="#1f1f26",
                           activebackground="#111115",
                           activeforeground="#38bdf8",
                           font=("Monospace", 9), anchor="w"
                           ).pack(fill="x")

        # --- frequency / channel ---
        freq_box = tk.LabelFrame(right, text=" 2.4 GHz CHANNEL ", bg="#111115",
                                 fg="#38bdf8", font=("Monospace", 8, "bold"),
                                 highlightbackground="#27272a", highlightthickness=1,
                                 padx=8, pady=6)
        freq_box.pack(fill="x", pady=2)

        row = tk.Frame(freq_box, bg="#111115")
        row.pack(fill="x", pady=(0, 4))
        tk.Label(row, text="CHANNEL:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold"), width=9, anchor="w"
                 ).pack(side="left")
        self.ch_var = tk.IntVar(value=76)
        self.ch_slider = tk.Scale(row, from_=0, to=125, orient="horizontal",
                                  variable=self.ch_var,
                                  bg="#111115", fg="#e4e4e7",
                                  troughcolor="#1f1f26",
                                  highlightthickness=0, showvalue=True,
                                  command=self._on_ch_slide)
        self.ch_slider.pack(side="left", fill="x", expand=True)

        row2 = tk.Frame(freq_box, bg="#111115")
        row2.pack(fill="x")
        self.freq_lbl = tk.Label(row2, text="2400 + 76 = 2476 MHz",
                                 fg="#38bdf8", bg="#111115",
                                 font=("Monospace", 9, "bold"))
        self.freq_lbl.pack(side="left")

        row3 = tk.Frame(freq_box, bg="#111115")
        row3.pack(fill="x", pady=(4, 0))
        tk.Label(row3, text="PRESETS:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold")).pack(side="left")
        for lbl, ch in [("BLE 2", 2), ("BLE 26", 26), ("BLE 80", 80),
                        ("WiFi 1", 1), ("WiFi 6", 6), ("WiFi 11", 11),
                        ("Zigbee 11", 11), ("Zigbee 26", 26)]:
            tk.Button(row3, text=lbl,
                      command=lambda c=ch: self._set_channel(c),
                      bg="#18181b", fg="#d4d4d8", relief="flat",
                      font=("Monospace", 7, "bold"), padx=4, pady=1
                      ).pack(side="left", padx=1)

        # --- listen / scan / triple ---
        listen_box = tk.LabelFrame(right, text=" RECEIVE ", bg="#111115",
                                   fg="#4ade80", font=("Monospace", 8, "bold"),
                                   highlightbackground="#27272a",
                                   highlightthickness=1, padx=8, pady=6)
        listen_box.pack(fill="x", pady=2)
        rowL = tk.Frame(listen_box, bg="#111115")
        rowL.pack(fill="x")
        self.btn_listen = tk.Button(rowL, text="▶ LISTEN ON CH",
                                    command=self._toggle_listen,
                                    bg="#18181b", fg="#4ade80", relief="flat",
                                    font=("Monospace", 9, "bold"), padx=8, pady=3)
        self.btn_listen.pack(side="left", padx=2)
        self.btn_triple = tk.Button(rowL, text="▶ BLE TRIPLE (2/26/80)",
                                    command=self._toggle_triple,
                                    bg="#18181b", fg="#f472b6", relief="flat",
                                    font=("Monospace", 9, "bold"), padx=8, pady=3)
        self.btn_triple.pack(side="left", padx=2)
        tk.Button(rowL, text="SCAN RPD", command=self._do_scan,
                  bg="#18181b", fg="#38bdf8", relief="flat",
                  font=("Monospace", 9, "bold"), padx=8, pady=3
                  ).pack(side="left", padx=2)
        tk.Button(rowL, text="■ STOP", command=self._stop_all,
                  bg="#18181b", fg="#f87171", relief="flat",
                  font=("Monospace", 9, "bold"), padx=8, pady=3
                  ).pack(side="left", padx=2)

        # --- continuous carrier ---
        carrier_box = tk.LabelFrame(right, text=" CONTINUOUS CARRIER ",
                                     bg="#111115", fg="#f43f5e",
                                     font=("Monospace", 8, "bold"),
                                     highlightbackground="#27272a",
                                     highlightthickness=1, padx=8, pady=6)
        carrier_box.pack(fill="x", pady=2)
        rowC = tk.Frame(carrier_box, bg="#111115")
        rowC.pack(fill="x")
        tk.Label(rowC, text="RADIO:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.carrier_radio_var = tk.IntVar(value=0)
        for i, lbl in enumerate(("NRF1", "NRF2", "NRF3")):
            tk.Radiobutton(rowC, text=lbl, variable=self.carrier_radio_var,
                           value=i, bg="#111115", fg="#e4e4e7",
                           selectcolor="#1f1f26", activebackground="#111115",
                           activeforeground="#f43f5e",
                           font=("Monospace", 8, "bold")).pack(side="left", padx=2)
        self.btn_carrier = tk.Button(rowC, text="CARRIER ON",
                                      command=self._toggle_carrier,
                                      bg="#18181b", fg="#f43f5e", relief="flat",
                                      font=("Monospace", 9, "bold"),
                                      padx=8, pady=3)
        self.btn_carrier.pack(side="left", padx=6)
        self.btn_carrier_tri = tk.Button(rowC, text="TRIPLE CARRIER",
                                          command=self._toggle_carrier_triple,
                                          bg="#18181b", fg="#fb7185", relief="flat",
                                          font=("Monospace", 9, "bold"),
                                          padx=8, pady=3)
        self.btn_carrier_tri.pack(side="left", padx=2)
        self.carrier_status = tk.Label(carrier_box, text="OFF",
                                        fg="#71717a", bg="#111115",
                                        font=("Monospace", 8, "bold"))
        self.carrier_status.pack(anchor="w", pady=(3, 0))

        rate_box = tk.Frame(right, bg="#09090b")
        rate_box.pack(fill="x", pady=(0, 2))
        self.rate_lbl = tk.Label(rate_box, text="FLOOD RATE: 0 pkt/s",
                                  fg="#f59e0b", bg="#09090b",
                                  font=("Monospace", 9, "bold"))
        self.rate_lbl.pack(side="left")

        # --- transmit ---
        tx_box = tk.LabelFrame(right, text=" TRANSMIT ", bg="#111115",
                               fg="#f59e0b", font=("Monospace", 8, "bold"),
                               highlightbackground="#27272a", highlightthickness=1,
                               padx=8, pady=6)
        tx_box.pack(fill="x", pady=2)
        rowT = tk.Frame(tx_box, bg="#111115")
        rowT.pack(fill="x")
        tk.Label(rowT, text="RATE:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.rate_combo = ttk.Combobox(rowT, values=list(RATES.keys()),
                                       width=10, state="readonly",
                                       font=("Monospace", 9))
        self.rate_combo.current(1)  # 2 Mbps
        self.rate_combo.pack(side="left", padx=4)
        self.rate_combo.bind("<<ComboboxSelected>>",
                             lambda _e: self._on_rate_change())

        rowT2 = tk.Frame(tx_box, bg="#111115")
        rowT2.pack(fill="x", pady=(4, 0))
        tk.Label(rowT2, text="HEX PAYLOAD:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace", 9, "bold")).pack(side="left")
        self.payload_entry = tk.Entry(rowT2, bg="#141418", fg="#f4f4f5",
                                      insertbackground="#f59e0b",
                                      font=("Monospace", 9), width=40)
        self.payload_entry.insert(0, "45 45 45 45 45")  # default
        self.payload_entry.pack(side="left", padx=4, fill="x", expand=True)
        tk.Button(rowT2, text="SEND ONE", command=self._send_tx,
                  bg="#92400e", fg="white", relief="flat",
                  font=("Monospace", 9, "bold"), padx=8
                  ).pack(side="right")

        # --- packet log ---
        log_box = tk.LabelFrame(right, text=" PACKET LOG ", bg="#111115",
                                fg="#e4e4e7", font=("Monospace", 8, "bold"),
                                highlightbackground="#27272a",
                                highlightthickness=1, padx=4, pady=4)
        log_box.pack(fill="both", expand=True, pady=2)
        self.log = tk.Text(log_box, height=7, bg="#0f0f13", fg="#d4d4d8",
                           insertbackground="#f472b6",
                           font=("Monospace", 8), relief="flat",
                           highlightthickness=0, padx=4, pady=2)
        self.log.pack(fill="both", expand=True)
        self._build_ir_tab(ir_tab)

        # footer
        foot = tk.Frame(self.root, bg="#09090b")
        foot.pack(fill="x", padx=8, pady=(2, 8))
        tk.Button(foot, text="CLOSE", command=self._on_close,
                  bg="#18181b", fg="#f87171", relief="flat",
                  font=("Monospace", 9, "bold"), padx=12, pady=4
                  ).pack(side="right")

    # ---------------- IR tab ----------------
    def _build_ir_tab(self, parent):
        hdr = tk.Frame(parent, bg="#111115", highlightbackground="#dc2626",
                       highlightthickness=1)
        hdr.pack(fill="x", pady=(4, 2))
        tk.Label(hdr, text="IR BLASTER", fg="#f87171", bg="#111115",
                 font=("Monospace", 11, "bold")).pack(side="left", padx=8, pady=4)
        self.ir_mode_lbl = tk.Label(hdr, text="NRF3: RADIO MODE",
                                     fg="#f59e0b", bg="#111115",
                                     font=("Monospace", 8, "bold"))
        self.ir_mode_lbl.pack(side="right", padx=8)

        lib = tk.Frame(parent, bg="#09090b")
        lib.pack(fill="x", pady=2)
        for lbl, cmd in [("LOAD .IR", self._ir_load), ("SAVE .IR", self._ir_save),
                         ("DELETE", self._ir_delete), ("CLEAR ALL", self._ir_clear)]:
            fg = "#38bdf8" if ".IR" in lbl else "#f87171"
            tk.Button(lib, text=lbl, command=cmd, bg="#18181b", fg=fg,
                      relief="flat", font=("Monospace", 8, "bold"),
                      padx=8, pady=2).pack(side="left", padx=2)

        act = tk.Frame(parent, bg="#09090b")
        act.pack(fill="x", pady=2)
        self.ir_rec_btn = tk.Button(act, text="● REC", command=self._ir_toggle_rec,
                                     bg="#18181b", fg="#f87171", relief="flat",
                                     font=("Monospace", 9, "bold"), padx=10, pady=3)
        self.ir_rec_btn.pack(side="left", padx=2)
        self.ir_play_btn = tk.Button(act, text="▶ REPLAY", command=self._ir_replay,
                                      bg="#14532d", fg="#4ade80", relief="flat",
                                      font=("Monospace", 9, "bold"), padx=10, pady=3)
        self.ir_play_btn.pack(side="left", padx=2)
        tk.Button(act, text="⇄ TOGGLE NRF3/IR", command=self._ir_toggle_mode,
                  bg="#18181b", fg="#f59e0b", relief="flat",
                  font=("Monospace", 8, "bold"), padx=8, pady=3).pack(side="left", padx=2)
        self.ir_status = tk.Label(act, text="idle", fg="#a1a1aa", bg="#09090b",
                                   font=("Monospace", 8, "bold"))
        self.ir_status.pack(side="right", padx=6)

        listf = tk.Frame(parent, bg="#27272a", padx=1, pady=1)
        listf.pack(fill="both", expand=True, pady=2)
        self.ir_listbox = tk.Listbox(listf, bg="#121216", fg="#e4e4e7",
                                      selectbackground="#dc2626",
                                      selectforeground="white",
                                      font=("Monospace", 9), relief="flat",
                                      highlightthickness=0, activestyle="none")
        self.ir_listbox.pack(fill="both", expand=True, padx=4, pady=4)

        self.ir_log = tk.Text(parent, height=4, bg="#0f0f13", fg="#d4d4d8",
                               font=("Monospace", 8), relief="flat",
                               highlightbackground="#27272a",
                               highlightthickness=1, padx=6, pady=4)
        self.ir_log.pack(fill="x", pady=(2, 0))

    def _ir_log(self, msg):
        self.ir_log.insert(tk.END, f"{time.strftime('%H:%M:%S')} {msg}\n")
        self.ir_log.see(tk.END)
        n = int(self.ir_log.index("end-1c").split(".")[0])
        if n > 200:
            self.ir_log.delete("1.0", f"{n-200}.0")

    def _ir_refresh(self):
        self.ir_listbox.delete(0, tk.END)
        for i, s in enumerate(self.ir_signals):
            freq = s.get("frequency", 38000)
            np = len(s.get("data", []))
            self.ir_listbox.insert(tk.END,
                f"{i:02d}  {s.get('name', f'SIG_{i}'):24s} {freq:>6d}Hz  {np}p")

    def _ir_load(self):
        p = filedialog.askopenfilename(title="Load .ir file",
            filetypes=[("IR signals", "*.ir"), ("All", "*.*")])
        if not p: return
        try:
            self.ir_signals = parse_ir_file(p)
            self._ir_refresh()
            self._ir_log(f"[+] Loaded {len(self.ir_signals)} signals from {os.path.basename(p)}")
        except Exception as e:
            self._ir_log(f"[!] Load failed: {e}")

    def _ir_save(self):
        if not self.ir_signals: return
        p = filedialog.asksaveasfilename(defaultextension=".ir",
            initialfile="icecream_export.ir",
            filetypes=[("IR signals", "*.ir")])
        if p:
            export_ir_file(p, self.ir_signals)
            self._ir_log(f"[+] Saved to {os.path.basename(p)}")

    def _ir_delete(self):
        sel = self.ir_listbox.curselection()
        if not sel: return
        removed = self.ir_signals.pop(sel[0])
        self._ir_refresh()
        self._ir_log(f"[-] Deleted {removed.get('name','?')}")

    def _ir_clear(self):
        self.ir_signals.clear()
        self._ir_refresh()
        self._ir_log("[*] Cleared all signals")

    def _ir_toggle_mode(self):
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first"); return
        target = not self.ir_mode
        ack = self.link.send_ir_mode(target)
        if ack == b"K":
            self.ir_mode = target
            self.ir_mode_lbl.config(
                text=f"NRF3: {'IR MODE' if target else 'RADIO MODE'}",
                fg="#f87171" if target else "#f59e0b")
            self._ir_log(f"[*] NRF3 -> {'IR' if target else 'RADIO'}")
        else:
            self._ir_log(f"[!] G toggle failed (ack={ack!r})")

    def _ir_toggle_rec(self):
        if self.ir_recording:
            self.ir_recording = False
            self.link.send_ir_json({"cmd": "ir_rx_stop"})
            self.ir_rec_btn.config(text="● REC", bg="#18181b", fg="#f87171")
            self.ir_status.config(text="idle", fg="#a1a1aa")
            return
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first"); return
        if not self.ir_mode:
            self._ir_log("[!] Toggle NRF3 -> IR first")
            return
        self.ir_recording = True
        self.ir_rec_btn.config(text="■ STOP", bg="#dc2626", fg="white")
        self.ir_status.config(text="recording...", fg="#f87171")
        self.link.send_ir_json({"cmd": "ir_rx_start"})
        threading.Thread(target=self._ir_rec_worker, daemon=True).start()

    def _ir_rec_worker(self):
        pulses = self.link.send_ir_recv_burst(timeout=6.0)
        self.link.send_ir_json({"cmd": "ir_rx_stop"})
        self.ir_recording = False
        self.root.after(0, lambda: self.ir_rec_btn.config(
            text="● REC", bg="#18181b", fg="#f87171"))
        if pulses:
            name = f"IR_REC_{len(self.ir_signals)+1}"
            self.ir_signals.append({"name": name, "type": "raw",
                                     "frequency": 38000, "duty_cycle": 0.33,
                                     "data": pulses})
            self.root.after(0, self._ir_refresh)
            self.root.after(0, lambda: self.ir_status.config(
                text=f"saved {len(pulses)}p", fg="#4ade80"))
            self.root.after(0, lambda: self._ir_log(f"[+] Captured {len(pulses)} pulses as {name}"))
        else:
            self.root.after(0, lambda: self.ir_status.config(text="no pulses", fg="#facc15"))

    def _ir_replay(self):
        sel = self.ir_listbox.curselection()
        if not sel:
            self._ir_log("[!] Select a signal first"); return
        sig = self.ir_signals[sel[0]]
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first"); return
        if not self.ir_mode:
            self._ir_log("[!] Toggle NRF3 -> IR first")
            return
        pkt = {"cmd": "ir_tx", "name": sig.get("name", "SIG"),
               "type": sig.get("type", "raw"),
               "frequency": int(sig.get("frequency", 38000)),
               "duty_cycle": float(sig.get("duty_cycle", 0.33)),
               "data": sig.get("data", [])}
        self.link.send_ir_json(pkt, timeout=2.5)
        self._ir_log(f"[>] Replayed {pkt['name']} ({len(pkt['data'])} pulses)")

    # ---- sizing ----------------------------------------------------------
    def _size_to_content(self):
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(),  720)
        h = max(self.root.winfo_reqheight(), 560)
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(w, h)

    # ---- port / connection ----------------------------------------------
    def _autodetect_port(self):
        self._refresh_ports()
        ports = list(self.port_combo["values"])
        if ports:
            self.port_combo.current(0)
            self.root.after(200, self._toggle_conn)

    def _refresh_ports(self):
        ports = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
        self.port_combo["values"] = ports
        if ports and not self.port_combo.get():
            self.port_combo.current(0)

    def _toggle_conn(self):
        if self.link.is_open:
            self._stop_all()
            self.link.close()
            self.conn_btn.config(text="CONNECT", bg="#dc2626")
            self.status_lbl.config(text="OFFLINE", fg="#a1a1aa", bg="#1f1f26")
            self._log("[*] Disconnected")
            return
        port = self.port_combo.get()
        if not port:
            messagebox.showwarning("No Port", "Pick a serial port first")
            return
        ok, msg = self.link.open(port)
        if not ok:
            messagebox.showerror("Serial Error", f"{port}:\n{msg}")
            return
        self.conn_btn.config(text="DISCONNECT", bg="#27272a")
        self.status_lbl.config(text=f"ONLINE: {os.path.basename(port)}",
                               fg="#4ade80", bg="#14291d")
        self._log(f"[+] Connected to {port}")
        self.link.send_u(self.selected_nrf)

    # ---- radio helpers ---------------------------------------------------
    def _on_nrf_change(self):
        self.selected_nrf = self.nrf_var.get()
        if self.link.is_open:
            ok = self.link.send_u(self.selected_nrf)
            self._log(f"[*] NRF{self.selected_nrf + 1} select -> {'OK' if ok else 'FAIL'}")
        self._stop_listen()

    def _on_ch_slide(self, _v=None):
        ch = self.ch_var.get()
        self.freq_lbl.config(text=f"2400 + {ch} = {ch_to_mhz(ch)} MHz")
        if self.listen_mode == "single" and self.link.is_open:
            self.link.send_m(ch)

    def _set_channel(self, ch):
        self.ch_var.set(ch)
        self._on_ch_slide()

    def _on_rate_change(self):
        self.tx_rate = RATES.get(self.rate_combo.get(), 1)

    # ---- listen / scan / stop -------------------------------------------
    def _toggle_listen(self):
        if self.listen_mode == "single":
            self._stop_listen()
            return
        self._stop_listen()
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first")
            return
        ok = self.link.send_m(self.ch_var.get())
        if not ok:
            self._log(f"[!] M failed on ch {self.ch_var.get()}")
            return
        self.listen_mode = "single"
        self.btn_listen.config(text="■ STOP LISTEN", bg="#7e22ce", fg="white")
        self._log(f"[*] Listening on CH {self.ch_var.get()} ({ch_to_mhz(self.ch_var.get())} MHz)")
        self._start_rx_poll()

    def _toggle_triple(self):
        if self.listen_mode == "triple":
            self._stop_listen()
            return
        self._stop_listen()
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first")
            return
        c0, c1, c2 = BLE_TRIPLE
        ok = self.link.send_t(c0, c1, c2)
        if not ok:
            self._log("[!] T (triple) failed")
            return
        self.listen_mode = "triple"
        self.btn_triple.config(text="■ STOP TRIPLE", bg="#9d174d", fg="white")
        self._log(f"[*] Triple-listening BLE {c0}/{c1}/{c2} "
                  f"({ch_to_mhz(c0)}/{ch_to_mhz(c1)}/{ch_to_mhz(c2)} MHz)")
        self._start_rx_poll()

    # ---------------- BLE advertising flood ----------------
    def _toggle_flood_triple(self):
        """Flood the three BLE advertising channels (37/38/39) simultaneously.
        Mouse/keyboard HID reconnects hit those channels and get stepped on,
        which trips the supervision timeout and drops the link."""
        if self.flood_thread and self.flood_thread.is_alive():
            self._stop_flood()
            return
        self._stop_listen()
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first")
            return
        self.stop_flood.clear()
        self.btn_flood_tri.config(text="STOP FLOOD", bg="#7c2d12", fg="white")
        self._log("[*] FLOOD ADV starting on BLE 37/38/39 (2402/2426/2480)")
        self.flood_thread = threading.Thread(
            target=self._flood_worker_triple, daemon=True)
        self.flood_thread.start()

    def _toggle_flood_sweep(self):
        """Sweep all 37 BLE data channels (0-36) as fast as the firmware will
        accept N commands. Slower per-channel than triple, but catches an
        established connection when it hops."""
        if self.flood_thread and self.flood_thread.is_alive():
            self._stop_flood()
            return
        self._stop_listen()
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first")
            return
        self.stop_flood.clear()
        self.btn_flood_sweep.config(text="STOP SWEEP", bg="#6b21a8", fg="white")
        self._log("[*] FLOOD SWEEP starting on BLE data ch 0-36")
        self.flood_thread = threading.Thread(
            target=self._flood_worker_sweep, daemon=True)
        self.flood_thread.start()

    def _start_rate_ticker(self):
        def _update():
            if self.stop_flood.is_set() and not (self.flood_thread and self.flood_thread.is_alive()):
                self.rate_lbl.config(text="FLOOD RATE: 0 pkt/s")
                return
            elapsed = time.monotonic() - self.flood_t0
            rate = int(self.flood_pkt_count / elapsed) if elapsed > 0 else 0
            self.rate_lbl.config(text=f"FLOOD RATE: {rate} pkt/s  ({self.flood_pkt_count} total)")
            self.root.after(500, _update)
        self.root.after(500, _update)

    def _toggle_carrier(self):
        radio = self.carrier_radio_var.get()
        ch = self.ch_var.get()
        if radio in self.carrier_active:
            ack = self.link.send_carrier_stop()
            self.carrier_active.pop(radio, None)
            self.btn_carrier.config(text="CARRIER ON", bg="#18181b", fg="#f43f5e")
            self._update_carrier_status()
            self._log(f"[CARRIER] stopped (ack={ack!r})")
        else:
            if not self.link.is_open:
                messagebox.showwarning("Offline", "Connect first")
                return
            ack = self.link.send_carrier(ch, radio)
            if ack == b"K":
                self.carrier_active[radio] = ch
                self.btn_carrier.config(text="CARRIER OFF", bg="#9f1239", fg="white")
                self._log(f"[CARRIER] NRF{radio+1} @ CH{ch} ({ch_to_mhz(ch)} MHz)")
                self._update_carrier_status()
            else:
                self._log(f"[!] Carrier start failed (ack={ack!r})")

    def _toggle_carrier_triple(self):
        if self.carrier_active:
            self.link.send_carrier_stop()
            self.carrier_active.clear()
            self.btn_carrier_tri.config(text="TRIPLE CARRIER", bg="#18181b", fg="#fb7185")
            self._update_carrier_status()
            self._log("[CARRIER] triple stopped")
            return
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first")
            return
        ok_count = 0
        for radio, ch in enumerate(BLE_TRIPLE):
            if self.link.send_carrier(ch, radio) == b"K":
                self.carrier_active[radio] = ch
                ok_count += 1
        if ok_count > 0:
            self.btn_carrier_tri.config(text="STOP TRIPLE", bg="#9f1239", fg="white")
            self._log(f"[CARRIER] {ok_count}/3 radios jamming BLE adv 2/26/80")
            self._update_carrier_status()
        else:
            self._log("[!] Triple carrier failed on all radios")

    def _update_carrier_status(self):
        if not self.carrier_active:
            self.carrier_status.config(text="OFF", fg="#71717a")
        else:
            parts = [f"NRF{r+1}:CH{c}" for r, c in sorted(self.carrier_active.items())]
            self.carrier_status.config(text="ON  " + " | ".join(parts), fg="#f43f5e")

    def _stop_flood(self):
        self.stop_flood.set()
        self.btn_flood_tri.config(text="FLOOD ADV 3CH",
                                  bg="#18181b", fg="#f59e0b")
        self.btn_flood_sweep.config(text="FLOOD SWEEP 0-36",
                                    bg="#18181b", fg="#a855f7")

    def _flood_worker_triple(self):
        """Rotate the active NRF between the three advertising channels so
        each one sees a burst at full rate — same result as having 3 radios
        transmit simultaneously, but reuses your single-U command firmware."""
        payload = bytes([0xFF] * 32)
        chans = list(BLE_TRIPLE)
        i = 0
        self.flood_pkt_count = 0
        self.flood_t0 = time.monotonic()
        self._start_rate_ticker()
        while not self.stop_flood.is_set():
            ch = chans[i % 3]
            self.link.send_u(i % 3)
            if self.link.send_n(ch, 1, payload) == b"D":
                self.flood_pkt_count += 1
            i += 1

    def _flood_worker_sweep(self):
        """Sweep channels 0..36, one TX per channel per pass."""
        payload = bytes([0xFF] * 32)
        self.flood_pkt_count = 0
        self.flood_t0 = time.monotonic()
        self._start_rate_ticker()
        while not self.stop_flood.is_set():
            for ch in range(37):
                if self.stop_flood.is_set():
                    return
                if self.link.send_n(ch, 1, payload) == b"D":
                    self.flood_pkt_count += 1

    def _stop_all(self):
        self._stop_flood()
        self._stop_listen()
        if self.carrier_active:
            self.link.send_carrier_stop()
            self.carrier_active.clear()
            self.btn_carrier.config(text="CARRIER ON", bg="#18181b", fg="#f43f5e")
            self.btn_carrier_tri.config(text="TRIPLE CARRIER", bg="#18181b", fg="#fb7185")
            self._update_carrier_status()
        if self.link.is_open:
            self.link.send_stop()
        self._log("[*] Stopped all activity")

    def _stop_listen(self):
        self.stop_rx.set()
        if self.listen_mode is not None and self.link.is_open:
            self.link.send_stop()
        self.listen_mode = None
        self.btn_listen.config(text="▶ LISTEN ON CH", bg="#18181b", fg="#4ade80")
        self.btn_triple.config(text="▶ BLE TRIPLE (2/26/80)", bg="#18181b", fg="#f472b6")

    def _do_scan(self):
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first")
            return
        self._stop_listen()

        def worker():
            bits = self.link.send_scan()
            if not bits:
                self.root.after(0, lambda: self._log("[!] Scan returned empty"))
                return
            hits = [i for i, b in enumerate(bits) if b == "1"]
            msg = (f"[SCAN] {len(hits)} active channels: "
                   + ", ".join(f"CH{c}({ch_to_mhz(c)}MHz)" for c in hits[:16])
                   + ("..." if len(hits) > 16 else ""))
            self.root.after(0, lambda: self._log(msg))
        threading.Thread(target=worker, daemon=True).start()

    def _start_rx_poll(self):
        self.stop_rx.clear()
        if self.rx_thread and self.rx_thread.is_alive():
            return
        self.rx_thread = threading.Thread(target=self._rx_poll_worker, daemon=True)
        self.rx_thread.start()

    def _rx_poll_worker(self):
        while not self.stop_rx.is_set():
            hex_pkt = self.link.read_packet(timeout=0.2)
            if hex_pkt:
                self.root.after(0, lambda h=hex_pkt: self._log(f"[PKT] {h[:48]}"))
            else:
                time.sleep(0.05)

    # ---- transmit --------------------------------------------------------
    def _send_tx(self):
        if not self.link.is_open:
            messagebox.showwarning("Offline", "Connect first")
            return
        txt = self.payload_entry.get().strip()
        clean = re.sub(r"[^0-9A-Fa-f]", "", txt)
        if len(clean) % 2:
            clean += "0"
        try:
            payload = bytes.fromhex(clean)[:32]
        except ValueError:
            messagebox.showerror("Bad Payload", "Payload must be hex bytes")
            return
        ch = self.ch_var.get()
        ack = self.link.send_n(ch, self.tx_rate, payload)
        if ack == b"D":
            self._log(f"[TX] CH{ch} ({ch_to_mhz(ch)}MHz) rate={REV_RATES[self.tx_rate]} "
                      f"len={len(payload)} OK")
        else:
            self._log(f"[TX] CH{ch} FAILED (ack={ack!r})")

    # ---- OLED nav --------------------------------------------------------
    def _oled_prev(self):
        self.oled.prev()
        self._sync_mode_to_radio()

    def _oled_next(self):
        self.oled.next()
        self._sync_mode_to_radio()

    def _oled_select(self):
        self.oled.toggle_active()
        mode = self.oled.current_mode
        if self.oled.active:
            self._log(f"[OLED] ACTIVE: {mode}")
            if mode == "BLE":
                self._set_channel(BLE_TRIPLE[0])
            elif mode == "Bluetooth":
                self._set_channel(BLE_TRIPLE[1])
        else:
            self._log(f"[OLED] DEACTIVE: {mode}")
            self._stop_all()

    def _sync_mode_to_radio(self):
        """Map the OLED menu cursor to a sensible default channel."""
        mode = self.oled.current_mode
        defaults = {
            "WiFi": 1, "Video TX": 50, "RC": 20,
            "BLE": BLE_TRIPLE[0], "Bluetooth": BLE_TRIPLE[1],
            "USB Wireless": 40, "Zigbee": 11, "NRF24": 76,
        }
        self._set_channel(defaults.get(mode, 76))

    # ---- logging ---------------------------------------------------------
    def _log(self, msg):
        self.log.insert(tk.END, f"{time.strftime('%H:%M:%S')} {msg}\n")
        self.log.see(tk.END)
        n = int(self.log.index("end-1c").split(".")[0])
        if n > 500:
            self.log.delete("1.0", f"{n-500}.0")

    # ---- shutdown --------------------------------------------------------
    def _on_close(self):
        try:
            self._stop_flood()
            self._stop_all()
            self.link.close()
        finally:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    IceCreamApp(root)
    root.mainloop()

# === ICECREAM.PY PATCH BLOCK: FLOOD WIDGET GUARD ===
def _ic_ensure_flood_buttons(app):
    parent = getattr(app, "root", None)
    if parent is None:
        return
    from tkinter import ttk as _ic_ttk
    if not hasattr(app, "btn_flood_tri") and hasattr(app, "_toggle_flood_triple"):
        app.btn_flood_tri = _ic_ttk.Button(parent, text="Flood Triple", command=app._toggle_flood_triple)
        app.btn_flood_tri.pack(side="left", padx=2)
    if not hasattr(app, "btn_flood_sweep") and hasattr(app, "_toggle_flood_sweep"):
        app.btn_flood_sweep = _ic_ttk.Button(parent, text="Flood Sweep", command=app._toggle_flood_sweep)
        app.btn_flood_sweep.pack(side="left", padx=2)
