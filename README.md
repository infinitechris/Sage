# Sage 🧭

A local desktop companion dashboard, RSS parser, and synchronization engine for **esPod** (the C++ Cardputer podcast player firmware). Sage manages your subscriptions, automates audio downloads, generates cleanly sorted playlists, and seamlessly syncs on-device playback history back into its database off an SD card.

## ✨ Features

* **Desktop Subscription Dashboard:** dead-simple web dashboard for listing, adding, and deactivating podcast feeds.
* **Smart Filter & Keyword Auto-Archiving:** Define custom skipping words per show (e.g. *Bonus*, *Recap*, *Live*) to keep clutter out of your queue.
* **Auto-Archive Retention Limits:** Configure a granular archive threshold (from 1 day up to 4 months, or disabled) so older episodes are auto-retired to save local and SD storage.
* **Atomic JSON Storage protection:** Uses low-level `os.replace` operations during disk updates to prevent 0-byte or corrupted JSON states if a transfer is interrupted.
* **Chrono-Priority M3U Generator:** Auto-generates a master queue playlist (`Podcasts/playlist.m3u`) ordered with your star-marked priority shows first, followed by remaining shows sorted chronologically.
* **Comprehensive History Ingestion Loop:** Reads `/state.json` off your esPod's SD card, ingests played flags and timestamps, purges finished MP3s, and updates the local web chronological History logs.

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- Flask (`requirements.txt`)

### Installation & Run

1. Navigate to the project folder:
   ```bash
   cd Sage
   ```
2. Run the application:
   ```bash
   venv/bin/python sage.py
   ```
3. Open your browser and navigate to `http://127.0.0.1:5000`

---

## 💾 SD Card Manifest Standard

Sage compiles and staged SD structures following the [SD Card Manifest Spec](docs/SD_MANIFEST_SPEC.md) so both python-side utilities and esPod's Arduino FAT32 libraries can traverse them with zero lag.