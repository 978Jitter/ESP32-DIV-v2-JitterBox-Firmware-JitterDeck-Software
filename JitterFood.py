#!/usr/bin/env python3
"""JitterFood2 — credential pipeline for JitterDeck3 / JitterBox."""

import os, re, sys, json, glob, time, hashlib, threading, subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime

try:
    import serial
except ImportError:
    serial = None

STRICT_MODE = False

STORE_DIR  = os.path.expanduser("~/.jitterfood")
MENU_PATH  = os.path.join(STORE_DIR, "menu.json")
PACKET_DIR = os.path.join(STORE_DIR, "packets")
SCRAPE_DIR = os.path.join(STORE_DIR, "scrapes")
RF_PATH    = os.path.join(STORE_DIR, "rf_captures.json")
EVENTS_LOG = os.path.join(STORE_DIR, "events.log")
STATUS     = os.path.join(STORE_DIR, "status.json")
REWARDS    = os.path.join(STORE_DIR, "rewards.json")
LOCK_PATH  = os.path.join(STORE_DIR, "link.lock")

SCRAPE_PRESETS = {
    "Full scan":        {"usernames":True,"passwords":True,"emails":True,"hashes":True,
                         "hosts":True,"urls":True,"tokens":True,"phones":False,
                         "wallets":False,"apikeys":True},
    "Credentials only": {"usernames":True,"passwords":True,"emails":True,"hashes":True,
                         "hosts":False,"urls":False,"tokens":False,"phones":False,
                         "wallets":False,"apikeys":False},
    "Secrets":          {"usernames":False,"passwords":False,"emails":False,"hashes":False,
                         "hosts":False,"urls":False,"tokens":True,"phones":False,
                         "wallets":True,"apikeys":True},
    "Recon":            {"usernames":False,"passwords":False,"emails":False,"hashes":False,
                         "hosts":True,"urls":True,"tokens":False,"phones":True,
                         "wallets":False,"apikeys":False},
    "Custom regex":     {},
}

SCRAPE_RULES = {
    "usernames": re.compile(r"(?im)\b(?:user(?:name)?|login|usr)\s*[:=]\s*['\"]?([A-Za-z0-9_.\-\\@]{2,64})"),
    "passwords": re.compile(r"(?im)\b(?:pass(?:word|wd)?|pwd)\s*[:=]\s*['\"]?([^\s'\"]{1,128})"),
    "emails":    re.compile(r"(?im)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,63}\b"),
    "hashes":    re.compile(r"(?im)\b([0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})\b"),
    "hosts":     re.compile(r"(?im)\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[a-z0-9\-]+(?:\.[a-z0-9\-]+){1,4}\.(?:local|lan|corp|com|net|org|io)\b"),
    "urls":      re.compile(r"(?im)\bhttps?://[^\s'\"<>\)]+"),
    "tokens":    re.compile(r"(?im)\b(?:bearer|token|api[_\-]?key|secret|session(?:id)?)\s*[:=]\s*['\"]?([A-Za-z0-9._\-]{8,256})"),
    "phones":    re.compile(r"(?im)\+?\b\d{1,3}[\s.\-]?\(?\d{2,4}\)?[\s.\-]?\d{3,4}[\s.\-]?\d{3,4}\b"),
    "wallets":   re.compile(r"(?im)\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|0x[a-fA-F0-9]{40})\b"),
    "apikeys":   re.compile(r"(?im)\b(?:AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"),
}

CRED_KEYS = {
    "user":("user","username","usr","login"), "pass":("pass","password","pwd"),
    "domain":("domain","dom","realm","workgroup"), "host":("host","ip","target","computer","machine"),
    "hash":("hash","ntlm","lm","nt"), "admin":("admin","is_admin","domain_admin","da"),
}

DEFAULT_REWARDS = {
    "xp":{"EMAILS":1,"USERNAMES":1,"HOSTS":1,"URLS":1,"PHONES":1,"WIFI":1,
          "PASSWORDS":5,"TOKENS":6,"HASHES":8,"APIKEYS":10,"WALLETS":12,"DOMAIN_ADMIN":15,
          "CREDCRACK_USERPASS":3,"CREDCRACK_HASH":8,"CREDCRACK_ADMIN":15,
          "RF-PULSE":2,"NRF24":2,"RAW-RF":2,"MANUAL":0,"CUSTOM":0},
    "kind_multiplier":{"hap":1.0,"hmg":1.0,"raw":1.0},
    "first_time_bonus":2.0,
    "first_time_tags":["APIKEYS","WALLETS","DOMAIN_ADMIN"],
    "no_refarm":True,
}


def ensure_dirs():
    for d in (STORE_DIR, PACKET_DIR, SCRAPE_DIR):
        os.makedirs(d, exist_ok=True)

def atomic_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default

def sha8(s):
    return hashlib.sha1(s.encode("utf-8","ignore")).hexdigest()[:8]

def open_folder(path):
    path = os.path.expanduser(path)
    os.makedirs(path, exist_ok=True)
    try:
        if sys.platform.startswith("win"): os.startfile(path)
        elif sys.platform == "darwin": subprocess.Popen(["open", path])
        else: subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


class CredParser:
    _kv = re.compile(r"(?P<k>[A-Za-z_ ]{2,20})\s*[:=]\s*(?P<v>[^\s,;]+(?:\s+[^\s,;:]+)?)", re.IGNORECASE)
    @classmethod
    def parse(cls, line):
        line = (line or "").strip()
        if not line or line.startswith("#"): return None
        out = {}
        m = re.search(r"(?P<u>[^:\s]+):(?P<p>[^@\s]+)@(?P<h>[\w.\-]+)", line)
        if m:
            out["user"],out["pass"],out["host"] = m.group("u"),m.group("p"),m.group("h")
        for km in cls._kv.finditer(line):
            key = km.group("k").strip().lower().replace(" ","_")
            val = km.group("v").strip().strip(",;")
            if not val: continue
            for canon, aliases in CRED_KEYS.items():
                if key in aliases:
                    if canon == "admin":
                        out[canon] = val.lower() in ("1","true","yes","y","da")
                    else:
                        out.setdefault(canon, val)
                    break
        hm = re.search(r"\b([0-9A-Fa-f]{32,})\b", line)
        if hm and "hash" not in out: out["hash"] = hm.group(1)
        if not out.get("user") and not out.get("pass") and not out.get("hash"): return None
        return out


class CredCrackFeeder(threading.Thread):
    def __init__(self, script, ip, on_line, on_status):
        super().__init__(daemon=True)
        self.script, self.ip = script, ip
        self.on_line, self.on_status = on_line, on_status
        self.stop_evt = threading.Event()
        self.proc = None

    def _python2(self):
        for c in ("python2","python2.7","/usr/bin/python2"):
            try:
                subprocess.check_output([c,"--version"],stderr=subprocess.STDOUT)
                return c
            except Exception: continue
        return None

    def run(self):
        py2 = self._python2()
        if not py2:
            self.on_status("No python2 on PATH","WARN"); return
        if not os.path.isfile(self.script):
            self.on_status(f"Missing: {self.script}","ERR"); return
        cmd = [py2, self.script, "-t", self.ip] if self.ip else [py2, self.script]
        self.on_status(f"Running: {' '.join(cmd)}","OK")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, bufsize=1,
                                         universal_newlines=True)
        except Exception as exc:
            self.on_status(f"Spawn failed: {exc}","ERR"); return
        for raw in self.proc.stdout:
            if self.stop_evt.is_set(): break
            line = raw.rstrip("\r\n")
            if not line: continue
            self.on_line(line, CredParser.parse(line))
        try: self.proc.wait(timeout=1.0)
        except Exception: pass
        self.on_status("Feed ended","IDLE")

    def stop(self):
        self.stop_evt.set()
        if self.proc and self.proc.poll() is None:
            try: self.proc.terminate()
            except Exception: pass


class JitterFood2:
    def __init__(self, root):
        self.root = root
        self.root.title("JitterFood2 // Credential Pipeline")
        self.root.geometry("520x640")
        self.root.configure(bg="#09090b")
        self.root.minsize(480, 560)

        ensure_dirs()
        self.menu = load_json(MENU_PATH, {"next_id":1,"items":[]})
        self.menu.setdefault("next_id",1)
        self.menu.setdefault("items",[])
        self.rewards = self._load_rewards()
        self.lock = threading.Lock()

        self.ser = None
        self.ser_lock = threading.Lock()
        self.credcrack = None
        self.cc_feeding = tk.BooleanVar(value=False)

        self.filter = tk.StringVar(value="")
        self.preset = tk.StringVar(value="Full scan")
        self.regex = tk.StringVar(value="")
        self.kind_override = tk.StringVar(value="auto")
        self.badge_text = tk.StringVar(value="0 items")
        self.strict_mode = STRICT_MODE

        self.nrf_unit = tk.StringVar(value="NRF1")
        self.last_rssi = None
        self.wifi_networks = []
        self.iface = tk.StringVar(value="wlan0")
        self.cc_script = tk.StringVar(value="credcrack.py")
        self.cc_ip = tk.StringVar(value="")

        self._build_ui()
        self._refresh_menu()
        self._refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_rewards(self):
        d = load_json(REWARDS, None)
        if d is None:
            atomic_json(REWARDS, DEFAULT_REWARDS)
            return json.loads(json.dumps(DEFAULT_REWARDS))
        d.setdefault("xp",{})
        d.setdefault("kind_multiplier",{"hap":1.0,"hmg":1.0,"raw":1.0})
        d.setdefault("first_time_bonus",2.0)
        d.setdefault("first_time_tags",["APIKEYS","WALLETS","DOMAIN_ADMIN"])
        d.setdefault("no_refarm",True)
        return d

    def _xp_for(self, tag, kind, reuse):
        xp_t = self.rewards.get("xp",{})
        base = xp_t.get((tag or "").upper(),1)
        mult = float(self.rewards.get("kind_multiplier",{}).get(kind,1.0))
        xp = base * mult
        if (tag or "").upper() in self.rewards.get("first_time_tags",[]) and reuse == 1:
            xp *= float(self.rewards.get("first_time_bonus",1.0))
        if self.rewards.get("no_refarm",True) and reuse > 1:
            xp = 0
        return int(round(xp))

    def _log(self, msg):
        self.console.insert(tk.END, msg + "\n")
        n = int(self.console.index("end-1c").split(".")[0])
        if n > 300: self.console.delete("1.0", f"{n-300}.0")
        self.console.see(tk.END)

    def _badge(self):
        self.badge_text.set(f"{len(self.menu['items'])} items")

    def _build_ui(self):
        hdr = tk.Frame(self.root, bg="#111115", height=32,
                       highlightbackground="#27272a", highlightthickness=1)
        hdr.pack(fill="x", padx=8, pady=(6,3)); hdr.pack_propagate(False)
        tk.Label(hdr, text="JITTERFOOD2", fg="#f59e0b", bg="#111115",
                 font=("Monospace",10,"bold")).pack(side="left", padx=8)
        tk.Label(hdr, textvariable=self.badge_text, fg="#4ade80", bg="#111115",
                 font=("Monospace",8,"bold")).pack(side="left", padx=6)
        tk.Button(hdr, text="📁", command=lambda: open_folder(STORE_DIR),
                  bg="#1f2937", fg="#38bdf8", activebackground="#0284c7",
                  activeforeground="white", relief="flat",
                  font=("Monospace",9,"bold"), padx=6, pady=1).pack(side="right", padx=4)

        link = tk.Frame(self.root, bg="#09090b"); link.pack(fill="x", padx=8, pady=2)
        tk.Label(link, text="PORT:", fg="#a1a1aa", bg="#09090b",
                 font=("Monospace",8,"bold")).pack(side="left")
        self.port_cb = ttk.Combobox(link, width=14, state="readonly",
                                    font=("Monospace",8))
        self.port_cb.pack(side="left", padx=3)
        tk.Button(link, text="↻", command=self._refresh_ports,
                  bg="#18181b", fg="#d4d4d8", relief="flat",
                  font=("Monospace",9,"bold"), width=2).pack(side="left", padx=2)
        self.conn_btn = tk.Button(link, text="CONNECT", command=self._toggle_link,
                                  bg="#dc2626", fg="white", activebackground="#b91c1c",
                                  activeforeground="white", relief="flat",
                                  font=("Monospace",8,"bold"), padx=8, pady=1)
        self.conn_btn.pack(side="right")

        # Scraper
        sc = tk.Frame(self.root, bg="#111115", highlightbackground="#27272a",
                      highlightthickness=1, padx=6, pady=4)
        sc.pack(fill="x", padx=8, pady=2)
        r1 = tk.Frame(sc, bg="#111115"); r1.pack(fill="x")
        tk.Label(r1, text="PRESET:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace",8,"bold")).pack(side="left")
        ttk.Combobox(r1, textvariable=self.preset, values=list(SCRAPE_PRESETS.keys()),
                     state="readonly", width=16, font=("Monospace",8)
                     ).pack(side="left", padx=4)
        tk.Label(r1, text="KIND:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace",8,"bold")).pack(side="left")
        ttk.Combobox(r1, textvariable=self.kind_override,
                     values=["auto","hmg","hap","raw"], state="readonly",
                     width=6, font=("Monospace",8)).pack(side="left", padx=4)

        r2 = tk.Frame(sc, bg="#111115"); r2.pack(fill="x", pady=(4,0))
        tk.Label(r2, text="TARGET:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace",8,"bold")).pack(side="left")
        self.target_e = tk.Entry(r2, bg="#0f0f13", fg="#f4f4f5",
                                 insertbackground="#ef4444", font=("Monospace",8),
                                 relief="flat", highlightthickness=1,
                                 highlightbackground="#27272a")
        self.target_e.pack(side="left", fill="x", expand=True, padx=3)
        tk.Button(r2, text="📄", command=self._pick_file, bg="#18181b",
                  fg="#d4d4d8", relief="flat", font=("Monospace",8), padx=4
                  ).pack(side="left", padx=1)
        tk.Button(r2, text="📁", command=self._pick_folder, bg="#18181b",
                  fg="#d4d4d8", relief="flat", font=("Monospace",8), padx=4
                  ).pack(side="left", padx=1)

        r3 = tk.Frame(sc, bg="#111115"); r3.pack(fill="x", pady=(4,0))
        tk.Label(r3, text="REGEX:", fg="#71717a", bg="#111115",
                 font=("Monospace",8,"bold")).pack(side="left")
        tk.Entry(r3, textvariable=self.regex, bg="#0f0f13", fg="#f4f4f5",
                 insertbackground="#ef4444", font=("Monospace",8),
                 relief="flat", highlightthickness=1,
                 highlightbackground="#27272a").pack(side="left", fill="x", expand=True, padx=3)
        tk.Button(r3, text="SCRAPE", command=self._do_scrape, bg="#3b1118",
                  fg="#f4f4f5", activebackground="#7f1d1d",
                  activeforeground="white", relief="flat",
                  font=("Monospace",8,"bold"), padx=8).pack(side="left", padx=2)
        tk.Button(r3, text="PASTE", command=self._paste_scrape, bg="#1f2937",
                  fg="#38bdf8", activebackground="#0284c7",
                  activeforeground="white", relief="flat",
                  font=("Monospace",8,"bold"), padx=6).pack(side="left", padx=2)

        # CredCrack
        cc = tk.Frame(self.root, bg="#111115", highlightbackground="#27272a",
                      highlightthickness=1, padx=6, pady=4)
        cc.pack(fill="x", padx=8, pady=2)
        tk.Checkbutton(cc, text="CREDCRACK", variable=self.cc_feeding,
                       command=self._toggle_cc, bg="#111115", fg="#38bdf8",
                       selectcolor="#18181b", activebackground="#111115",
                       activeforeground="white", font=("Monospace",8,"bold")
                       ).pack(side="left")
        tk.Entry(cc, textvariable=self.cc_script, width=14, bg="#0f0f13",
                 fg="#f4f4f5", insertbackground="#ef4444", font=("Monospace",8),
                 relief="flat", highlightthickness=1,
                 highlightbackground="#27272a").pack(side="left", padx=3)
        tk.Label(cc, text="IP:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace",8)).pack(side="left")
        tk.Entry(cc, textvariable=self.cc_ip, width=16, bg="#0f0f13",
                 fg="#f4f4f5", insertbackground="#ef4444", font=("Monospace",8),
                 relief="flat", highlightthickness=1,
                 highlightbackground="#27272a").pack(side="left", padx=3)

        # WiFi
        wf = tk.Frame(self.root, bg="#111115", highlightbackground="#27272a",
                      highlightthickness=1, padx=6, pady=4)
        wf.pack(fill="x", padx=8, pady=2)
        tk.Label(wf, text="WIFI:", fg="#a1a1aa", bg="#111115",
                 font=("Monospace",8,"bold")).pack(side="left")
        ttk.Combobox(wf, textvariable=self.iface,
                     values=["wlan0","wlan1","wlan2"], state="readonly",
                     width=6, font=("Monospace",8)).pack(side="left", padx=3)
        tk.Button(wf, text="SCAN", command=self._wifi_scan, bg="#18181b",
                  fg="#38bdf8", relief="flat", font=("Monospace",8,"bold"),
                  padx=6).pack(side="left", padx=3)
        self.wifi_list = tk.Listbox(wf, height=3, bg="#0f0f13", fg="#e4e4e7",
                                    selectbackground="#dc2626",
                                    selectforeground="white",
                                    font=("Monospace",7), relief="flat",
                                    highlightthickness=1,
                                    highlightbackground="#27272a")
        self.wifi_list.pack(side="left", fill="x", expand=True, padx=3)
        tk.Button(wf, text="USE", command=self._wifi_use, bg="#14532d",
                  fg="#4ade80", relief="flat", font=("Monospace",8,"bold"),
                  padx=6).pack(side="left", padx=2)

        # Menu toolbar
        bar = tk.Frame(self.root, bg="#09090b"); bar.pack(fill="x", padx=8, pady=(4,2))
        tk.Label(bar, text="MENU:", fg="#a1a1aa", bg="#09090b",
                 font=("Monospace",9,"bold")).pack(side="left")
        tk.Entry(bar, textvariable=self.filter, width=18, bg="#0f0f13",
                 fg="#f4f4f5", insertbackground="#ef4444", font=("Monospace",8),
                 relief="flat", highlightthickness=1,
                 highlightbackground="#27272a").pack(side="left", padx=4)
        self.filter.trace_add("write", lambda *_: self._refresh_menu())
        self.menu_btn = tk.Menubutton(bar, text="ACTIONS ▾", bg="#1f2937",
                                      fg="#38bdf8", activebackground="#0284c7",
                                      activeforeground="white", relief="flat",
                                      font=("Monospace",8,"bold"), padx=6, pady=1)
        m = tk.Menu(self.menu_btn, tearoff=0, bg="#18181b", fg="#e4e4e7",
                    font=("Monospace",8))
        m.add_command(label="USE AS FOOD", command=self._menu_use)
        m.add_command(label="Delete", command=self._menu_delete)
        m.add_command(label="Clear all", command=self._menu_clear)
        m.add_separator()
        m.add_command(label="Export JSON", command=self._menu_export)
        self.menu_btn["menu"] = m
        self.menu_btn.pack(side="right", padx=2)

        # Menu tree
        tf = tk.Frame(self.root, bg="#27272a", padx=1, pady=1)
        tf.pack(fill="both", expand=True, padx=8, pady=2)
        cols = ("kind","tag","payload","src","uses")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=10,
                                 selectmode="browse")
        for c,t,w in (("kind","K",36),("tag","TAG",90),("payload","PAYLOAD",220),
                      ("src","SRC",70),("uses","X",30)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=(c=="payload"),
                             anchor="w" if c in ("tag","payload") else "center")
        self.tree.tag_configure("hmg", foreground="#f59e0b")
        self.tree.tag_configure("hap", foreground="#38bdf8")
        self.tree.tag_configure("raw", foreground="#a1a1aa")
        self.tree.pack(fill="both", expand=True)

        # Console
        self.console = tk.Text(self.root, height=4, bg="#0f0f13", fg="#d4d4d8",
                               insertbackground="#ef4444", font=("Monospace",8),
                               relief="flat", highlightbackground="#27272a",
                               highlightthickness=1, padx=6, pady=3)
        self.console.pack(fill="x", padx=8, pady=(2,6))
        self._log(f"[*] JitterFood2 online. Vault: {STORE_DIR}")
        self._log(f"[*] strict_mode = {self.strict_mode}")

    def _refresh_menu(self):
        self.tree.delete(*self.tree.get_children())
        f = self.filter.get().strip().lower()
        for it in self.menu["items"]:
            blob = " ".join(str(it.get(k,"")) for k in ("kind","tag","payload","source")).lower()
            if f and f not in blob: continue
            self.tree.insert("", tk.END, iid=str(it["id"]),
                             values=(it.get("kind","raw").upper()[:1],
                                     it.get("tag","?"),
                                     it.get("payload","")[:80],
                                     it.get("source","?")[:14],
                                     it.get("reuse",0)),
                             tags=(it.get("kind","raw"),))
        self._badge()

    def _selected(self):
        s = self.tree.selection()
        if not s: return None
        iid = s[0]
        for it in self.menu["items"]:
            if str(it["id"]) == str(iid): return it
        return None

    def _persist(self, item):
        with self.lock:
            if not item.get("id"):
                item["id"] = self.menu["next_id"]; self.menu["next_id"] += 1
            key = (item.get("kind"), item.get("tag"), item.get("payload"))
            for ex in self.menu["items"]:
                if (ex.get("kind"),ex.get("tag"),ex.get("payload")) == key:
                    ex["reuse"] = ex.get("reuse",0) + 1
                    atomic_json(MENU_PATH, self.menu)
                    self._xp_send("FOOD_DEDUPE", ex, self._xp_for(
                        ex.get("tag"), ex.get("kind"), ex.get("reuse",1)))
                    self.root.after(0, self._refresh_menu)
                    return ex
            self.menu["items"].append(item)
            atomic_json(MENU_PATH, self.menu)
        self._xp_send("FOOD_SCRAPED", item, self._xp_for(
            item.get("tag"), item.get("kind"), 1))
        self.root.after(0, self._refresh_menu)
        return item

    def _xp_send(self, event, item, xp):
        xp = int(xp)
        line = (f"ROBOT_EVENT:{event}:{item.get('id')}:"
                f"{item.get('kind','raw').upper()}:"
                f"{item.get('tag','?')}:{xp}")
        try: print(line, flush=True)
        except Exception: pass
        try:
            with open(EVENTS_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{int(time.time())}\t{line}\n")
        except Exception: pass
        try:
            atomic_json(STATUS, {"items":len(self.menu["items"]),
                                 "packets":0,"updated":int(time.time())})
        except Exception: pass
        if xp > 0 and self.ser and getattr(self.ser,"is_open",False):
            try:
                with self.ser_lock:
                    self.ser.write(f"Y{xp}\n".encode("ascii"))
            except Exception: pass
        self.root.after(0, lambda: self._log(f"[XP] {event} {item.get('tag')} +{xp}"))

    def _refresh_ports(self):
        ports = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
        self.port_cb["values"] = ports
        if ports: self.port_cb.current(0)

    def _toggle_link(self):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try: self.ser.close()
                except Exception: pass
                self.ser = None
                self.conn_btn.config(text="CONNECT", bg="#dc2626")
                try:
                    if os.path.exists(LOCK_PATH): os.remove(LOCK_PATH)
                except Exception: pass
                self._log("[*] Link closed.")
                return
            port = self.port_cb.get()
            if not port:
                messagebox.showerror("No Port","No serial port selected."); return
            try:
                if os.path.exists(LOCK_PATH):
                    with open(LOCK_PATH) as fh: owner = fh.read().strip()
                    if owner and owner != port and os.path.exists(owner):
                        messagebox.showerror("Busy", f"{port} held by another app.")
                        return
                with open(LOCK_PATH,"w") as fh: fh.write(port)
                self.ser = serial.Serial(port, 115200, timeout=0.3)
                self.ser.dtr = False; self.ser.rts = False
                time.sleep(0.2)
                self.ser.reset_input_buffer()
                self.ser.write(b"V")
                banner = self.ser.readline().decode("utf-8","ignore").strip()
                self.conn_btn.config(text="DISCONNECT", bg="#27272a")
                self._log(f"[+] Linked {port}" + (f" -> {banner}" if banner else ""))
            except Exception as exc:
                try:
                    if os.path.exists(LOCK_PATH): os.remove(LOCK_PATH)
                except Exception: pass
                self.ser = None
                messagebox.showerror("Serial Error", f"{port}:\n{exc}")

    # ---- scraper ----
    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="Scrape file",
            filetypes=[("Text","*.txt *.log *.json *.csv *.md"),("All","*.*")])
        if p: self.target_e.delete(0, tk.END); self.target_e.insert(0, p)

    def _pick_folder(self):
        p = filedialog.askdirectory(title="Scrape folder")
        if p: self.target_e.delete(0, tk.END); self.target_e.insert(0, p)

    def _do_scrape(self):
        target = self.target_e.get().strip()
        if not target:
            try: txt = self.root.clipboard_get()
            except Exception: txt = ""
            if not txt.strip():
                messagebox.showinfo("Scrape","Set TARGET or use PASTE."); return
            self._scrape_text(txt, "clipboard"); return
        if os.path.isdir(target): self._scrape_folder(target)
        elif os.path.isfile(target): self._scrape_file(target)
        else: self._scrape_text(target, "inline")

    def _paste_scrape(self):
        try: txt = self.root.clipboard_get()
        except Exception: txt = ""
        if not txt.strip():
            messagebox.showinfo("Clipboard","Clipboard is empty."); return
        self._scrape_text(txt, "clipboard")

    def _scrape_text(self, text, source):
        enabled = SCRAPE_PRESETS.get(self.preset.get(), {})
        hits = []
        for name, pat in SCRAPE_RULES.items():
            if not enabled.get(name, False): continue
            seen = set()
            for m in pat.finditer(text):
                val = (m.group(1) if (m.lastindex and m.group(1)) else m.group(0)).strip()
                if not val or val in seen: continue
                seen.add(val)
                kind = "hmg" if name in ("passwords","hashes","tokens","apikeys") else "hap"
                hits.append({"kind":kind,"tag":name.upper(),"payload":val[:200]})
        if self.regex.get().strip():
            try:
                cp = re.compile(self.regex.get())
                seen = set()
                for m in cp.finditer(text):
                    val = m.group(0).strip()
                    if val and val not in seen:
                        seen.add(val)
                        hits.append({"kind":"raw","tag":"CUSTOM","payload":val[:200]})
            except re.error as exc:
                self._log(f"[!] Regex error: {exc}")
        if not hits:
            self._log(f"[i] {source}: 0 hits"); return
        for h in hits:
            kind = h["kind"]
            if self.kind_override.get() != "auto": kind = self.kind_override.get()
            item = {"id":None,"kind":kind,"tag":h["tag"],"payload":h["payload"],
                    "freq_hz":0,"source":f"scrape:{source}","preset":"",
                    "extra":{},"created":int(time.time()),"reuse":0}
            self._persist(item)
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            atomic_json(os.path.join(SCRAPE_DIR, f"scrape_{ts}_{sha8(source)}.json"),
                        {"source":source,"hits":hits})
        except Exception: pass
        self._log(f"[+] {source}: {len(hits)} hits stored")

    def _scrape_file(self, path):
        try:
            with open(path,"r",encoding="utf-8",errors="ignore") as fh:
                txt = fh.read()
        except Exception as exc:
            self._log(f"[!] Read error: {exc}"); return
        self._scrape_text(txt, os.path.basename(path))

    def _scrape_folder(self, folder):
        total = 0
        for root,_,files in os.walk(folder):
            for fn in files:
                if not fn.lower().endswith((".txt",".log",".json",".csv",".md",".ir",".sub",".raw")):
                    continue
                p = os.path.join(root,fn)
                try:
                    with open(p,"r",encoding="utf-8",errors="ignore") as fh:
                        txt = fh.read()
                except Exception: continue
                enabled = SCRAPE_PRESETS.get(self.preset.get(), {})
                local_hits = []
                for name, pat in SCRAPE_RULES.items():
                    if not enabled.get(name, False): continue
                    seen = set()
                    for m in pat.finditer(txt):
                        val = (m.group(1) if (m.lastindex and m.group(1)) else m.group(0)).strip()
                        if not val or val in seen: continue
                        seen.add(val)
                        kind = "hmg" if name in ("passwords","hashes","tokens","apikeys") else "hap"
                        local_hits.append({"kind":kind,"tag":name.upper(),"payload":val[:200]})
                for h in local_hits:
                    kind = h["kind"]
                    if self.kind_override.get() != "auto": kind = self.kind_override.get()
                    item = {"id":None,"kind":kind,"tag":h["tag"],"payload":h["payload"],
                            "freq_hz":0,"source":f"file:{fn}","preset":"",
                            "extra":{},"created":int(time.time()),"reuse":0}
                    self._persist(item)
                    total += 1
        self._log(f"[+] Folder: {total} hits")

    # ---- credcrack ----
    def _toggle_cc(self):
        if self.cc_feeding.get():
            if self.credcrack and self.credcrack.is_alive(): return
            self.credcrack = CredCrackFeeder(
                self.cc_script.get().strip(), self.cc_ip.get().strip(),
                on_line=self._on_cc_line, on_status=self._on_cc_status)
            self.credcrack.start()
        else:
            if self.credcrack:
                self.credcrack.stop(); self.credcrack = None

    def _on_cc_status(self, msg, level):
        self.root.after(0, lambda: self._log(f"[CREDCRACK] {msg}"))

    def _on_cc_line(self, raw, parsed):
        if parsed is None: return
        self.root.after(0, lambda: self._log(f"[CC] {raw[:120]}"))
        if parsed.get("admin"): tag = "DOMAIN_ADMIN"
        elif parsed.get("hash"): tag = "HASHES"
        else: tag = "PASSWORDS"
        payload = (f"{parsed.get('domain','')}\\{parsed.get('user','?')}:"
                   f"{parsed.get('pass','?')}@{parsed.get('host','')}")
        item = {"id":None,"kind":"hmg","tag":tag,"payload":payload[:200],
                "freq_hz":0,"source":"credcrack","preset":"",
                "extra":parsed,"created":int(time.time()),"reuse":0}
        self.root.after(0, lambda it=item: self._persist(it))

    # ---- wifi ----
    def _wifi_scan(self):
        iface = self.iface.get()
        try:
            subprocess.run(["nmcli","device","wifi","rescan"],
                           capture_output=True, timeout=8)
            r = subprocess.run(["nmcli","-t","-f","SSID,SECURITY,SIGNAL",
                                "device","wifi","list","ifname",iface],
                               capture_output=True, text=True, timeout=10)
            self.wifi_list.delete(0, tk.END)
            self.wifi_networks = []
            seen = set()
            for line in r.stdout.splitlines():
                parts = line.rsplit(":", 2)
                if len(parts) < 3: continue
                ssid, sec, sig = parts[0], parts[1], parts[2]
                if not ssid or ssid in seen: continue
                seen.add(ssid)
                self.wifi_networks.append({"ssid":ssid,"sec":sec or "OPEN","sig":sig})
                self.wifi_list.insert(tk.END, f"{ssid[:20]} [{sec or 'OPEN'}] {sig}%")
            self._log(f"[WiFi] {iface}: {len(self.wifi_networks)} APs")
        except FileNotFoundError:
            messagebox.showerror("nmcli missing","Install network-manager.")
        except Exception as exc:
            self._log(f"[!] WiFi error: {exc}")

    def _wifi_use(self):
        sel = self.wifi_list.curselection()
        if not sel: return
        net = self.wifi_networks[sel[0]]
        item = {"id":None,"kind":"hap","tag":"WIFI",
                "payload":f"SSID={net['ssid']} SEC={net['sec']} SIG={net['sig']}%",
                "freq_hz":0,"source":f"wifi:{self.iface.get()}","preset":"",
                "extra":net,"created":int(time.time()),"reuse":0}
        self._persist(item)

    # ---- menu actions ----
    def _menu_use(self):
        it = self._selected()
        if not it: return
        kind = it.get("kind","raw")
        if self.kind_override.get() != "auto": kind = self.kind_override.get()
        try:
            os.makedirs(os.path.join(STORE_DIR,"food"), exist_ok=True)
            path = os.path.join(STORE_DIR,"food", f"food_{it['id']}_{kind}.food")
            with open(path,"w") as fh:
                json.dump({"item":it,"kind":kind}, fh, indent=2)
        except Exception: pass
        it["reuse"] = it.get("reuse",0) + 1
        atomic_json(MENU_PATH, self.menu)
        self._xp_send("FOOD_USE", it, self._xp_for(it.get("tag"), kind, it["reuse"]))
        self._refresh_menu()

    def _menu_delete(self):
        it = self._selected()
        if not it: return
        with self.lock:
            self.menu["items"] = [x for x in self.menu["items"] if x["id"] != it["id"]]
            atomic_json(MENU_PATH, self.menu)
        self._refresh_menu()

    def _menu_clear(self):
        if not messagebox.askyesno("Clear","Delete all food items?"): return
        with self.lock:
            self.menu = {"next_id":1,"items":[]}
            atomic_json(MENU_PATH, self.menu)
        self._refresh_menu()

    def _menu_export(self):
        p = filedialog.asksaveasfilename(defaultextension=".json",
                                         initialfile="jitterfood_menu.json")
        if not p: return
        with open(p,"w") as fh: json.dump(self.menu, fh, indent=2, sort_keys=True)
        self._log(f"[+] Exported: {p}")

    def _on_close(self):
        if self.credcrack: self.credcrack.stop()
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try: self.ser.close()
                except Exception: pass
        try:
            if os.path.exists(LOCK_PATH): os.remove(LOCK_PATH)
        except Exception: pass
        self.root.destroy()


if __name__ == "__main__":
    ensure_dirs()
    root = tk.Tk()
    app = JitterFood2(root)
    root.mainloop()


# === JITTERFOOD.PY PATCH BLOCK: PID PORT LOCK ===
import json as _jf_json
import os as _jf_os
import time as _jf_time

_JF_SHARED_LOCK = _jf_os.path.expanduser("~/.jitter_port.lock")


def jf_is_port_locked_by_other(target_port=None):
    if not _jf_os.path.exists(_JF_SHARED_LOCK):
        return False
    try:
        with open(_JF_SHARED_LOCK, "r", encoding="utf-8") as f:
            info = _jf_json.load(f)
        pid = info.get("pid")
        locked_port = info.get("port")
        if pid and pid != _jf_os.getpid() and _jf_os.path.exists(f"/proc/{pid}"):
            if _jf_time.time() - float(info.get("ts", 0)) < 3600:
                if target_port is None or locked_port == target_port:
                    return True
    except Exception:
        return False
    return False
