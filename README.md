# // MK_SIGNAL (`JitterBox` / `JitterDeck`)

> **⚠️ WORK IN PROGRESS:** Active experimental firmware and desktop/cyberdeck control stack. Pinouts, UI layouts, and RF/IR modules are continuously evolving—expect breaking changes between builds.

A custom ESP32-S3 multi-radio firmware suite and Python control interface built for the **MKSignal / ESP32-DIV v2** hardware platform (`ESP32-S3-WROOM-1U`, 8MB PSRAM, 16MB Flash, 2.8" ILI9341 TFT touchscreen, CC1101 Sub-GHz, triple nRF24 2.4GHz transceivers, IR, NFC, and GPS).

> **Legal & Lab Use Notice:** Built strictly for authorized RF research, educational hardware experimentation, and personal lab testing.

---

### 🧩 Core Ecosystem & Modules

* **🖥️ The Deck (`JitterDeck`):** Python desktop and handheld cyberdeck control suite connecting over serial (`/dev/ttyUSB0`) for live waterfall signal visualization, pulse cleaning, RSSI gating, Sub-GHz capture/replay, and a drag-and-drop `.ir` blaster bar (play, record, save/export).
* **👾 Gamification (`JitterGotchi`):** Interactive on-device RF companion living on the 2.8" TFT display. Features progressive eye fragmentation (`FRAG:X` / `DFRAG` maintenance), backlight telemetry (`LITE:ON`/`LITE:OFF`), reactive eye animations triggered by RF RX/TX and IR blasts, and signal nourishment mechanics.
* **🍔 Food (Signal Nourishment):** Gamified RF & IR capture system that "feeds" `JitterGotchi` when scanning, sniffing, and logging live wireless packets and Sub-GHz bursts.
* **🍦 IceCream (*"I Scream"* Jammer):** Noise/interference testing module ("I Scream" across target bands) designed for controlled Faraday/lab shielding and receiver resilience testing.
* **🦆 `JitterDucky` (Native USB HID):** Dual-USB-C architecture leveraging the ESP32-S3's native USB interface for DuckyScript HID payload execution alongside the serial telemetry bridge.

---

### 🔌 Hardware Target
* **Board:** MKSignal / ESP32-DIV v2 (`ESP32-S3-WROOM-1U`)
* **Radios & Peripherals:** CC1101 (Sub-GHz), 3× nRF24L01+ (2.4GHz), IR TX/RX, NFC, GPS, 2.8" ILI9341 TFT + XPT2046 Touch
* **Dual USB-C:** Port A (`CP210x` UART for `JitterDeck` & flashing) + Port B (Native USB OTG for `JitterDucky` HID)

### 💻 Tested Targets
* **Host Cyberdecks:** Kali Linux ARM64 & Debian Bookworm (`Jitter OS`) on Hackberry Pi CM5 & ClockworkPi uConsole
* **Desktop / Flashing:** Fedora & Pop!_OS (`arduino-cli` / `esptool`)
