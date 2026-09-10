from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
import os
import sys
import json
import requests
import threading
import time
import curses
import datetime
import logging
from parser import parse_feed, extract_pub_timestamp, write_json_atomic
from sync import sync_to_sd, get_sync_status, generate_playlist as sync_generate_playlist, purge_played_archived_downloads, get_recent_errors as sync_get_recent_errors, clear_recent_errors as sync_clear_recent_errors, add_playback_log, retroactively_build_history, SD_MOUNT_PATH as DEFAULT_SD_MOUNT, MOCK_SD_PATH

# Silence the once-a-second /sync_status access log spam without hiding real errors/requests
class _SuppressSyncStatusLogs(logging.Filter):
    def filter(self, record):
        return "/sync_status" not in record.getMessage()

logging.getLogger("werkzeug").addFilter(_SuppressSyncStatusLogs())

app = Flask(__name__)
SD_PATH = "mock_sd"
SD_MOUNT_PATH = DEFAULT_SD_MOUNT

# Global backend status state dictionary
status_state = {
    "sync_status": "Ready",
    "sd_mounted": False,
    "sd_mount_path": SD_MOUNT_PATH,
    "last_sync_time": None,
    "active_downloads": 0,
    "total_feeds": 0,
    "active_feeds": 0,
    "priority_feeds": 0,
    "total_episodes": 0,
    "downloaded_episodes": 0,
    "unplayed_episodes": 0,
    "playlist_episodes": 0,
    "last_error": None,
    "recent_errors": [],
    "current_activity": "Idle",
    "last_updated": time.time()
}

MAX_RECENT_ERRORS = 20

state_lock = threading.Lock()
# Serializes all operations that read/write feed JSON files or the downloads
# folder (refresh, sync, single downloads, feed checks) to prevent the race
# condition where overlapping runs clobber each other's file writes.
feed_ops_lock = threading.Lock()

def log_error(message):
    """Records a backend error so it surfaces on the dashboard instead of only the terminal."""
    print(message)
    with state_lock:
        status_state["recent_errors"].insert(0, {"time": datetime.datetime.now().strftime("%H:%M:%S"), "message": message})
        del status_state["recent_errors"][MAX_RECENT_ERRORS:]
        status_state["last_error"] = message

def sanitize_name(name):
    return "".join(c for c in name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')

def format_eta(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s"

def generate_playlist(base_dir=SD_PATH, sd_mount=SD_MOUNT_PATH):
    """
    Generates master .m3u playlist at Podcasts/playlist.m3u,
    including only downloaded, active (unplayed and unarchived) episodes,
    ordered by pub_timestamp DESC with priority items first.
    """
    count = sync_generate_playlist(base_dir, sd_mount)
    with state_lock:
        status_state["playlist_episodes"] = count
    return count

def update_status_state():
    """
    Refreshes the global status_state dictionary with real-time indicators.
    """
    with state_lock:
        sd_mounted = os.path.exists(SD_MOUNT_PATH)
        sync_status = get_sync_status()
        
        total_feeds = 0
        active_feeds = 0
        priority_feeds = 0
        total_episodes = 0
        downloaded_episodes = 0
        unplayed_episodes = 0
        
        feeds_dir = os.path.join(SD_PATH, "feeds")
        downloads_dir = os.path.join(SD_PATH, "downloads")
        
        if os.path.exists(feeds_dir):
            for filename in os.listdir(feeds_dir):
                if not filename.endswith(".json"):
                    continue
                total_feeds += 1
                try:
                    with open(os.path.join(feeds_dir, filename), "r") as f:
                        feed_data = json.load(f)
                    if feed_data.get("active", True):
                        active_feeds += 1
                        if feed_data.get("priority", False):
                            priority_feeds += 1
                        
                        slug = feed_data.get("slug", filename[:-5])
                        podcast_dl_dir = os.path.join(downloads_dir, slug)
                        
                        for ep in feed_data.get("episodes", []):
                            total_episodes += 1
                            safe_ep_title = sanitize_name(ep.get("title", ""))
                            std_file = os.path.join(podcast_dl_dir, f"{safe_ep_title}.mp3")
                            prio_file = os.path.join(podcast_dl_dir, f"PRIORITY_{safe_ep_title}.mp3")
                            
                            is_dl = os.path.exists(std_file) or os.path.exists(prio_file) or ep.get("downloaded", False)
                            if is_dl:
                                downloaded_episodes += 1
                            if not ep.get("played", False) and not ep.get("archived", False):
                                unplayed_episodes += 1
                except Exception:
                    pass
                    
        # Check playlist track count
        playlist_file = os.path.join(SD_PATH, "Podcasts", "playlist.m3u")
        playlist_count = 0
        if os.path.exists(playlist_file):
            try:
                with open(playlist_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("#EXTINF:"):
                            playlist_count += 1
            except Exception:
                pass
        else:
            playlist_count = status_state.get("playlist_episodes", 0)

        status_state["sd_mounted"] = sd_mounted
        status_state["sync_status"] = sync_status
        status_state["total_feeds"] = total_feeds
        status_state["active_feeds"] = active_feeds
        status_state["priority_feeds"] = priority_feeds
        status_state["total_episodes"] = total_episodes
        status_state["downloaded_episodes"] = downloaded_episodes
        status_state["unplayed_episodes"] = unplayed_episodes
        status_state["playlist_episodes"] = playlist_count
        status_state["last_updated"] = time.time()

        # Merge in any new errors logged from sync.py so they show up on the dashboard too
        existing_keys = {(e["time"], e["message"]) for e in status_state["recent_errors"]}
        for err in sync_get_recent_errors():
            key = (err["time"], err["message"])
            if key not in existing_keys:
                status_state["recent_errors"].append(err)
                existing_keys.add(key)
        del status_state["recent_errors"][MAX_RECENT_ERRORS:]
        
    return status_state

def get_all_active_feeds():
    """
    Returns list of active feeds sorted by priority (priority feeds first) and title.
    """
    all_feeds = []
    feeds_dir = os.path.join(SD_PATH, "feeds")
    if os.path.exists(feeds_dir):
        for filename in sorted(os.listdir(feeds_dir)):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(feeds_dir, filename), "r") as f:
                        data = json.load(f)
                        if data.get("active", True):
                            all_feeds.append(data)
                except Exception as e:
                    log_error(f"Error loading {filename}: {e}")
                    
    # Sort with priority feeds first, then alphabetical by title
    all_feeds.sort(key=lambda f: (not f.get("priority", False), f.get("feed_title", "").lower()))
    return all_feeds

@app.route("/")
def index():
    playback_data = {}
    pb_file = os.path.join(SD_PATH, "playback.json")
    if os.path.exists(pb_file):
        try:
            with open(pb_file, "r") as f:
                playback_data = json.load(f)
        except Exception:
            pass
            
    all_feeds = get_all_active_feeds()
    update_status_state()
    return render_template("index.html", feeds=all_feeds, playback=playback_data, status_state=status_state)

@app.route("/add_feed", methods=["POST"])
def add_feed():
    url = request.form.get("feed_url")
    if url:
        parse_feed(url, SD_PATH)
        generate_playlist(SD_PATH, SD_MOUNT_PATH)
        update_status_state()
    return redirect(url_for('index'))

@app.route('/assets/<path:filename>')
def serve_asset(filename):
    return send_from_directory(os.path.join(SD_PATH, "assets"), filename)

@app.route("/update_feed/<path:feed_identifier>")
def update_feed(feed_identifier):
    if not feed_identifier:
        return redirect(url_for('index'))
        
    safe_slug = sanitize_name(feed_identifier)
    json_path = os.path.join(SD_PATH, "feeds", f"{safe_slug}.json")
    target_url = feed_identifier
    feed_title = feed_identifier
    
    if os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                d = json.load(f)
                target_url = d.get("feed_url", feed_identifier)
                feed_title = d.get("feed_title", safe_slug)
        except Exception:
            pass
            
    with state_lock:
        status_state["current_activity"] = f"Checking {feed_title}..."
    with feed_ops_lock:
        try:
            parse_feed(target_url, SD_PATH)
            generate_playlist(SD_PATH, SD_MOUNT_PATH)
        finally:
            with state_lock:
                status_state["current_activity"] = "Idle"
            update_status_state()
        
    return redirect(url_for('index'))

@app.route("/feed/<feed_name>")
def feed_detail(feed_name):
    safe_name = sanitize_name(feed_name)
    feed_data = {}
    file_path = os.path.join(SD_PATH, "feeds", f"{safe_name}.json")
    
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        feed_priority = feed_data.get("priority", False)
        # Dynamically verify actual file existence in the podcast subfolder
        podcast_downloads_dir = os.path.join(SD_PATH, "downloads", safe_name)
        for ep in feed_data.get("episodes", []):
            safe_ep_title = sanitize_name(ep.get("title", ""))
            ep_filepath = os.path.join(podcast_downloads_dir, f"{safe_ep_title}.mp3")
            ep_prio_filepath = os.path.join(podcast_downloads_dir, f"PRIORITY_{safe_ep_title}.mp3")
            
            actual_exists = os.path.exists(ep_filepath) or os.path.exists(ep_prio_filepath)
            if ep.get("downloaded") != actual_exists:
                ep["downloaded"] = actual_exists
                
            if "pub_timestamp" not in ep or not ep["pub_timestamp"]:
                ep["pub_timestamp"] = extract_pub_timestamp(ep)
            if "priority" not in ep:
                ep["priority"] = feed_priority
                
        # Sort episodes: priority first, then pub_timestamp DESC
        feed_data.get("episodes", []).sort(key=lambda e: (not e.get("priority", False), -e.get("pub_timestamp", 0)))
                
        # Save corrected state back to JSON
        write_json_atomic(file_path, feed_data, indent=2)
            
    return render_template("feed.html", feed=feed_data, feed_name=safe_name)

@app.route("/toggle_episode/<feed_name>/<int:ep_index>/<action>")
def toggle_episode(feed_name, ep_index, action):
    file_path = os.path.join(SD_PATH, "feeds", f"{feed_name}.json")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        if 0 <= ep_index < len(feed_data["episodes"]):
            if action in ["played", "archived"]:
                current = feed_data["episodes"][ep_index].get(action, False)
                new_state = not current
                feed_data["episodes"][ep_index][action] = new_state
                
                write_json_atomic(file_path, feed_data, indent=2)
                
                # Log action to History Log
                act_str = f"marked {action}" if new_state else f"marked un-{action}"
                add_playback_log(feed_data.get("feed_title", feed_name), feed_data["episodes"][ep_index].get("title", ""), act_str, "Sage Dashboard", mock_sd_path=SD_PATH)
                    
        generate_playlist(SD_PATH, SD_MOUNT_PATH)
        update_status_state()
                    
    return redirect(url_for('feed_detail', feed_name=feed_name))

@app.route("/toggle_episode_priority/<feed_name>/<int:ep_index>")
def toggle_episode_priority(feed_name, ep_index):
    file_path = os.path.join(SD_PATH, "feeds", f"{feed_name}.json")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        if 0 <= ep_index < len(feed_data["episodes"]):
            feed_data["episodes"][ep_index]["priority"] = not feed_data["episodes"][ep_index].get("priority", False)
            write_json_atomic(file_path, feed_data, indent=2)
                
        generate_playlist(SD_PATH, SD_MOUNT_PATH)
        update_status_state()
        
    return redirect(url_for('feed_detail', feed_name=feed_name))

@app.route("/remove_feed/<feed_name>")
def remove_feed(feed_name):
    safe_name = sanitize_name(feed_name)
    file_path = os.path.join(SD_PATH, "feeds", f"{safe_name}.json")
    
    print(f"Attempting to deactivate: {file_path}")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        feed_data["active"] = False
        
        write_json_atomic(file_path, feed_data, indent=2)
            
        if feed_data.get("image_file"):
            img_path = os.path.join(SD_PATH, "assets", feed_data["image_file"])
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except Exception:
                    pass
                
        generate_playlist(SD_PATH, SD_MOUNT_PATH)
        update_status_state()
                
    return redirect(url_for('index'))

@app.route("/toggle_autodownload/<feed_name>")
def toggle_autodownload(feed_name):
    file_path = os.path.join(SD_PATH, "feeds", f"{feed_name}.json")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        feed_data["auto_download"] = not feed_data.get("auto_download", False)
        
        write_json_atomic(file_path, feed_data, indent=2)
            
        update_status_state()
            
    return redirect(url_for('feed_detail', feed_name=feed_name))

@app.route("/download_episode/<path:feed_name>/<int:ep_index>")
def download_episode(feed_name, ep_index):
    safe_feed_name = sanitize_name(feed_name)

    def _download_worker():
        with state_lock:
            status_state["active_downloads"] += 1
        try:
            with feed_ops_lock:
                download_episode_internal(safe_feed_name, ep_index)
                generate_playlist(SD_PATH, SD_MOUNT_PATH)
        finally:
            with state_lock:
                status_state["active_downloads"] = max(0, status_state["active_downloads"] - 1)
                status_state["current_activity"] = "Idle"
            update_status_state()

    thread = threading.Thread(target=_download_worker)
    thread.start()
                    
    return redirect(url_for('feed_detail', feed_name=feed_name))

@app.route("/refresh_feeds", methods=["POST"])
def refresh_feeds():
    def _refresh_worker():
        refresh_all_feeds_and_download()
        update_status_state()

    thread = threading.Thread(target=_refresh_worker)
    thread.start()
    return "", 204

@app.route("/sync_card", methods=["POST"])
def sync_card():
    def _sync_worker():
        with state_lock:
            status_state["current_activity"] = "Syncing to esPod..."
        with feed_ops_lock:
            sync_to_sd()
            generate_playlist(SD_PATH, SD_MOUNT_PATH)
        with state_lock:
            status_state["last_sync_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status_state["current_activity"] = "Idle"
        update_status_state()

    thread = threading.Thread(target=_sync_worker)
    thread.start()
    return "", 204

@app.route("/sync_status")
def sync_status():
    st = update_status_state()
    return jsonify({
        "status": st["sync_status"],
        "sd_mounted": st["sd_mounted"],
        "state": st
    })

@app.route("/clear_errors", methods=["POST"])
def clear_errors():
    with state_lock:
        status_state["recent_errors"] = []
        status_state["last_error"] = None
    sync_clear_recent_errors()
    return "", 204

@app.route("/toggle_priority/<feed_name>")
def toggle_priority(feed_name):
    file_path = os.path.join(SD_PATH, "feeds", f"{feed_name}.json")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        new_priority = not feed_data.get("priority", False)
        feed_data["priority"] = new_priority
        
        # Thread priority to episodes that don't have an explicit override
        for ep in feed_data.get("episodes", []):
            ep["priority"] = new_priority
            
        write_json_atomic(file_path, feed_data, indent=2)
            
        generate_playlist(SD_PATH, SD_MOUNT_PATH)
        update_status_state()
            
    return redirect(request.referrer or url_for('feed_detail', feed_name=feed_name))

@app.route("/update_filters/<feed_name>", methods=["POST"])
def update_filters(feed_name):
    file_path = os.path.join(SD_PATH, "feeds", f"{feed_name}.json")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        feed_data["filter_string"] = request.form.get("filter_string", "")
        try:
            feed_data["auto_archive_days"] = int(request.form.get("auto_archive_days", 0))
        except ValueError:
            feed_data["auto_archive_days"] = 0
        
        # Check existing episodes against new filters right away
        filters = [f.strip().lower() for f in feed_data["filter_string"].split(",") if f.strip()]
        for ep in feed_data.get("episodes", []):
            title_lower = ep.get("title", "").lower()
            matches_filter = any(f in title_lower for f in filters)
            
            # Check for age threshold matching (skipped if 0 / Disabled)
            is_too_old = False
            pub_ts = ep.get("pub_timestamp", 0)
            if feed_data["auto_archive_days"] > 0 and pub_ts > 0:
                age_seconds = time.time() - pub_ts
                if age_seconds > (feed_data["auto_archive_days"] * 24 * 3600):
                    is_too_old = True
                    
            if matches_filter or is_too_old:
                # auto-archive setting must skip any files that were already marked as "played"
                if ep.get("played", False):
                    continue
                if not ep.get("archived", False):
                    ep["archived"] = True
                    reason = "filter matches" if matches_filter else f"older than {feed_data['auto_archive_days']}d"
                    add_playback_log(feed_data.get("feed_title", feed_name), ep.get("title", ""), f"auto-archived ({reason})", "Sage Dashboard", mock_sd_path=SD_PATH)
                
        write_json_atomic(file_path, feed_data, indent=2)
            
        generate_playlist(SD_PATH, SD_MOUNT_PATH)
        update_status_state()
            
    return redirect(url_for('feed_detail', feed_name=feed_name))

@app.route("/generate_playlist_route")
def generate_playlist_route():
    count = generate_playlist(SD_PATH, SD_MOUNT_PATH)
    update_status_state()
    return jsonify({"success": True, "tracks": count})

def refresh_all_feeds_and_download():
    """
    Re-fetches every active feed's RSS for new episodes and downloads any
    undownloaded, non-archived, unplayed episodes, ingesting them into the
    master playlist. Shared by the manual sync card and the background loop.
    """
    feeds_dir = os.path.join(SD_PATH, "feeds")
    if not os.path.exists(feeds_dir):
        return

    if not feed_ops_lock.acquire(blocking=False):
        # Another refresh/sync/download is already in flight; skip this run
        # rather than racing on the same feed JSON files and download folders.
        with state_lock:
            status_state["current_activity"] = "Skipped: another sync/refresh is already running"
        return

    try:
        with state_lock:
            status_state["current_activity"] = "Checking feeds for new episodes..."

        feed_files = [f for f in os.listdir(feeds_dir) if f.endswith(".json")]

        # Sort feed files so priority feeds are checked and parsed first
        def feed_sort_key(fn):
            try:
                with open(os.path.join(feeds_dir, fn), "r") as f:
                    d = json.load(f)
                    return (not d.get("priority", False), fn)
            except Exception:
                return (True, fn)

        feed_files.sort(key=feed_sort_key)

        for filename in feed_files:
            try:
                file_path = os.path.join(feeds_dir, filename)
                with open(file_path, "r") as f:
                    data = json.load(f)

                if data.get("active", True) and data.get("feed_url"):
                    parse_feed(data["feed_url"], SD_PATH)
            except Exception as e:
                log_error(f"Error checking feed {filename}, skipping: {e}")

        with state_lock:
            status_state["current_activity"] = "Downloading new eligible episodes..."

        for filename in feed_files:
            try:
                file_path = os.path.join(feeds_dir, filename)
                with open(file_path, "r") as f:
                    data = json.load(f)

                if data.get("active", True) and data.get("auto_download", False):
                    safe_feed_name = data.get("slug")
                    for idx, ep in enumerate(data.get("episodes", [])):
                        if not ep.get("downloaded", False) and not ep.get("archived", False) and not ep.get("played", False):
                            download_episode_internal(safe_feed_name, idx)
            except Exception as e:
                log_error(f"Error downloading episodes for {filename}, skipping: {e}")

        with state_lock:
            status_state["current_activity"] = "Cleaning up played/archived downloads..."
        purge_played_archived_downloads(SD_PATH)

        generate_playlist(SD_PATH, SD_MOUNT_PATH)
        with state_lock:
            status_state["current_activity"] = "Idle"
    finally:
        feed_ops_lock.release()

def background_sync_worker():
    # Wait for the initial 30-minute interval before running background auto-sync
    time.sleep(1800)
    while True:
        try:
            feeds_dir = os.path.join(SD_PATH, "feeds")
            if os.path.exists(feeds_dir):
                refresh_all_feeds_and_download()
                with state_lock:
                    status_state["current_activity"] = "Idle (Waiting for next cycle)"
            else:
                with state_lock:
                    status_state["current_activity"] = "Idle (No feeds found)"
        except Exception as e:
            with state_lock:
                status_state["current_activity"] = f"Auto-sync error: {str(e)}"
            log_error(f"Auto-sync error: {e}")
            
        update_status_state()
        time.sleep(1800)


def download_episode_internal(safe_feed_name, ep_index):
    file_path = os.path.join(SD_PATH, "feeds", f"{safe_feed_name}.json")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            feed_data = json.load(f)
            
        if 0 <= ep_index < len(feed_data["episodes"]):
            ep = feed_data["episodes"][ep_index]
            audio_url = ep.get("audio_url")
            if audio_url and not ep.get("downloaded"):
                podcast_downloads_dir = os.path.join(SD_PATH, "downloads", safe_feed_name)
                os.makedirs(podcast_downloads_dir, exist_ok=True)
                
                safe_ep_title = sanitize_name(ep.get("title", ""))
                is_prio = ep.get("priority", feed_data.get("priority", False))
                prefix = "PRIORITY_" if is_prio else ""
                ep_filename = f"{prefix}{safe_ep_title}.mp3"
                ep_filepath = os.path.join(podcast_downloads_dir, ep_filename)
                
                feed_title = feed_data.get("feed_title", safe_feed_name)
                ep_title = ep.get("title", "Unknown Episode")
                ep_date = ep.get("published", "")
                
                try:
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) esPod/1.0"}
                    response = requests.get(audio_url, headers=headers, stream=True, timeout=60, allow_redirects=True)
                    if response.status_code == 200:
                        total_bytes = int(response.headers.get("Content-Length", 0) or 0)
                        downloaded_bytes = 0
                        start_time = time.time()
                        last_update = 0.0
                        
                        with open(ep_filepath, "wb") as f_audio:
                            for chunk in response.iter_content(chunk_size=8192):
                                f_audio.write(chunk)
                                downloaded_bytes += len(chunk)
                                
                                now = time.time()
                                if now - last_update >= 0.5:
                                    last_update = now
                                    elapsed = now - start_time
                                    speed_bps = downloaded_bytes / elapsed if elapsed > 0 else 0
                                    
                                    detail = f"Downloading: {feed_title} — {ep_title}"
                                    if ep_date:
                                        detail += f" ({ep_date})"
                                    if total_bytes:
                                        percent = downloaded_bytes / total_bytes * 100
                                        detail += f" — {percent:.0f}%"
                                        if speed_bps > 0:
                                            eta_seconds = (total_bytes - downloaded_bytes) / speed_bps
                                            detail += f", ETA {format_eta(eta_seconds)}"
                                    else:
                                        detail += f" — {downloaded_bytes // 1024}KB"
                                        
                                    with state_lock:
                                        status_state["current_activity"] = detail
                                        
                        ep["downloaded"] = True
                        write_json_atomic(file_path, feed_data, indent=2)
                except Exception as e:
                    log_error(f"Download failed for '{ep.get('title', 'Unknown Episode')}': {e}")


# ==============================================================================
# Curses Dashboard UI Implementation (run_dashboard)
# ==============================================================================

def run_dashboard(stdscr=None):
    """
    Interactive curses dashboard interface displaying real-time SD card mount status,
    background sync progress, feed statistics, and active master playlist indicators.
    """
    if stdscr is None:
        curses.wrapper(run_dashboard)
        return

    # Initialize curses settings
    try:
        curses.curs_set(0)
    except Exception:
        pass
        
    stdscr.nodelay(True)
    stdscr.timeout(500)  # Non-blocking poll every 500ms
    
    # Initialize color pairs if terminal supports colors
    has_color = curses.has_colors()
    if has_color:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # Success / Mounted / Active
        curses.init_pair(2, curses.COLOR_RED, -1)     # Danger / Unmounted
        curses.init_pair(3, curses.COLOR_YELLOW, -1)  # Warning / Priority / Working
        curses.init_pair(4, curses.COLOR_CYAN, -1)    # Info / Headers
        curses.init_pair(5, curses.COLOR_MAGENTA, -1) # Special / Badges
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)  # Top title bar
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE) # Selected row highlight

    selected_feed_idx = 0
    scroll_offset = 0
    toast_message = "Welcome to Sage Dashboard! Press [H] for help."
    toast_time = time.time()
    spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    spinner_idx = 0

    def safe_addstr(y, x, text, attr=0):
        max_y, max_x = stdscr.getmaxyx()
        if y < 0 or y >= max_y or x < 0 or x >= max_x:
            return
        available = max_x - x
        if available <= 0:
            return
        clipped = text[:available]
        try:
            stdscr.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    while True:
        spinner_idx = (spinner_idx + 1) % len(spinner_frames)
        state = update_status_state()
        feeds = get_all_active_feeds()
        
        max_y, max_x = stdscr.getmaxyx()
        stdscr.erase()

        if max_y < 12 or max_x < 50:
            safe_addstr(0, 0, "Terminal window too small! Please enlarge.", curses.A_BOLD)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord('q'), ord('Q')):
                break
            time.sleep(0.2)
            continue

        # ----------------------------------------------------------------------
        # Header / Title Bar
        # ----------------------------------------------------------------------
        title = " SAGE PODCAST ENGINE & CARDPUTER SYNC "
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header_line = f"{title:<{max_x - len(now_str) - 2}}{now_str} "
        safe_addstr(0, 0, header_line[:max_x], curses.color_pair(6) if has_color else curses.A_REVERSE)

        # ----------------------------------------------------------------------
        # Real-time Status Indicators Banner
        # ----------------------------------------------------------------------
        # 1. SD Card Mount Indicator
        sd_mounted = state.get("sd_mounted", False)
        sd_text = f" SD CARD: {'● MOUNTED (' + state.get('sd_mount_path', '') + ')' if sd_mounted else '○ UNMOUNTED (Disconnected)'}"
        sd_color = curses.color_pair(1) if (has_color and sd_mounted) else (curses.color_pair(2) if has_color else curses.A_BOLD)
        safe_addstr(2, 2, sd_text, sd_color | curses.A_BOLD)

        # 2. Background Sync Progress Indicator
        sync_status = state.get("sync_status", "Ready")
        current_activity = state.get("current_activity", "Idle")
        is_syncing = "Syncing" in sync_status or "Processing" in sync_status or "Reconciling" in sync_status or "Downloading" in current_activity
        
        spin_char = spinner_frames[spinner_idx] if is_syncing else "✓"
        sync_disp = f" SYNC STATUS: [{spin_char}] {sync_status}"
        sync_color = curses.color_pair(3) if (has_color and is_syncing) else (curses.color_pair(1) if has_color else curses.A_NORMAL)
        safe_addstr(3, 2, sync_disp, sync_color | curses.A_BOLD)

        if current_activity and current_activity != "Idle":
            safe_addstr(4, 4, f"↳ Activity: {current_activity}", curses.color_pair(4) if has_color else curses.A_DIM)

        # ----------------------------------------------------------------------
        # Summary Metrics Box
        # ----------------------------------------------------------------------
        stats_line = (
            f"Feeds: {state.get('active_feeds', 0)} ({state.get('priority_feeds', 0)} Priority) | "
            f"Episodes: {state.get('downloaded_episodes', 0)} Downloaded / {state.get('total_episodes', 0)} Total | "
            f"Queue: {state.get('unplayed_episodes', 0)} Unplayed | "
            f"Playlist: {state.get('playlist_episodes', 0)} Tracks"
        )
        safe_addstr(5, 2, stats_line, curses.color_pair(4) if has_color else curses.A_BOLD)
        safe_addstr(6, 0, "─" * max_x, curses.A_DIM)

        # ----------------------------------------------------------------------
        # Feeds List Table
        # ----------------------------------------------------------------------
        table_start_y = 7
        table_header = f" {'PRI':<5} {'PODCAST SHOW':<32} {'SLUG':<24} {'DL/UNPLAYED':<14} {'AUTO-DL':<8}"
        safe_addstr(table_start_y, 0, table_header, curses.A_BOLD | curses.A_UNDERLINE)

        visible_rows = max(1, max_y - table_start_y - 4)
        if selected_feed_idx >= len(feeds):
            selected_feed_idx = max(0, len(feeds) - 1)
        if selected_feed_idx < scroll_offset:
            scroll_offset = selected_feed_idx
        elif selected_feed_idx >= scroll_offset + visible_rows:
            scroll_offset = selected_feed_idx - visible_rows + 1

        for i in range(visible_rows):
            feed_i = scroll_offset + i
            row_y = table_start_y + 1 + i
            if feed_i < len(feeds):
                feed = feeds[feed_i]
                is_prio = feed.get("priority", False)
                prio_mark = " ★ " if is_prio else "   "
                title_str = feed.get("feed_title", "Unknown")[:30]
                slug_str = feed.get("slug", "")[:22]
                
                episodes = feed.get("episodes", [])
                unplayed_cnt = sum(1 for e in episodes if not e.get("played") and not e.get("archived"))
                dl_cnt = sum(1 for e in episodes if e.get("downloaded"))
                dl_stat = f"{dl_cnt}/{unplayed_cnt} ({len(episodes)})"
                auto_dl = "ON" if feed.get("auto_download", False) else "OFF"
                
                row_str = f" {prio_mark:<5} {title_str:<32} {slug_str:<24} {dl_stat:<14} {auto_dl:<8}"
                
                attr = curses.A_NORMAL
                if feed_i == selected_feed_idx:
                    attr = curses.color_pair(7) | curses.A_BOLD if has_color else curses.A_REVERSE
                elif is_prio:
                    attr = curses.color_pair(3) | curses.A_BOLD if has_color else curses.A_BOLD
                    
                safe_addstr(row_y, 0, row_str, attr)
            else:
                safe_addstr(row_y, 0, " " * max_x)

        # ----------------------------------------------------------------------
        # Toast / Notification Bar
        # ----------------------------------------------------------------------
        toast_y = max_y - 3
        safe_addstr(toast_y, 0, "─" * max_x, curses.A_DIM)
        if time.time() - toast_time < 8:
            safe_addstr(toast_y + 1, 2, f"ℹ {toast_message}", curses.color_pair(3) if has_color else curses.A_BOLD)
        else:
            safe_addstr(toast_y + 1, 2, f"Ready. Next auto-check in background cycle.", curses.A_DIM)

        # ----------------------------------------------------------------------
        # Bottom Controls Bar
        # ----------------------------------------------------------------------
        controls = " [S] Sync to SD  [R] Refresh All  [P] Gen Playlist  [T] Toggle Priority  [A] Auto-DL  [Q] Quit "
        safe_addstr(max_y - 1, 0, f"{controls:<{max_x}}", curses.color_pair(6) if has_color else curses.A_REVERSE)

        stdscr.refresh()

        # ----------------------------------------------------------------------
        # Input Handling
        # ----------------------------------------------------------------------
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1

        if ch == -1:
            continue

        if ch in (ord('q'), ord('Q'), 27):  # 27 = ESC
            break
        elif ch in (curses.KEY_UP, ord('k'), ord('K')):
            selected_feed_idx = max(0, selected_feed_idx - 1)
        elif ch in (curses.KEY_DOWN, ord('j'), ord('J')):
            selected_feed_idx = min(len(feeds) - 1, selected_feed_idx + 1)
        elif ch in (ord('s'), ord('S')):
            toast_message = "Initiating SD Card Sync in background..."
            toast_time = time.time()
            threading.Thread(target=lambda: (sync_to_sd(), generate_playlist(SD_PATH, SD_MOUNT_PATH), update_status_state()), daemon=True).start()
        elif ch in (ord('p'), ord('P')):
            cnt = generate_playlist(SD_PATH, SD_MOUNT_PATH)
            toast_message = f"Regenerated master playlist: {cnt} eligible tracks."
            toast_time = time.time()
        elif ch in (ord('r'), ord('R')):
            toast_message = "Triggering feed check & metadata update..."
            toast_time = time.time()
            def _refresh():
                for f in feeds:
                    if f.get("feed_url"):
                        parse_feed(f["feed_url"], SD_PATH)
                generate_playlist(SD_PATH, SD_MOUNT_PATH)
                update_status_state()
            threading.Thread(target=_refresh, daemon=True).start()
        elif ch in (ord('t'), ord('T')):
            if 0 <= selected_feed_idx < len(feeds):
                target_feed = feeds[selected_feed_idx]
                feed_slug = target_feed.get("slug")
                file_path = os.path.join(SD_PATH, "feeds", f"{feed_slug}.json")
                if os.path.exists(file_path):
                    with open(file_path, "r") as f:
                        d = json.load(f)
                    new_p = not d.get("priority", False)
                    d["priority"] = new_p
                    for ep in d.get("episodes", []):
                        ep["priority"] = new_p
                    write_json_atomic(file_path, d, indent=2)
                    generate_playlist(SD_PATH, SD_MOUNT_PATH)
                    update_status_state()
                    toast_message = f"Set '{target_feed.get('feed_title')}' priority: {'ON' if new_p else 'OFF'}"
                    toast_time = time.time()
        elif ch in (ord('a'), ord('A')):
            if 0 <= selected_feed_idx < len(feeds):
                target_feed = feeds[selected_feed_idx]
                feed_slug = target_feed.get("slug")
                file_path = os.path.join(SD_PATH, "feeds", f"{feed_slug}.json")
                if os.path.exists(file_path):
                    with open(file_path, "r") as f:
                        d = json.load(f)
                    d["auto_download"] = not d.get("auto_download", False)
                    write_json_atomic(file_path, d, indent=2)
                    update_status_state()
                    toast_message = f"Toggled Auto-Download for '{target_feed.get('feed_title')}' to {d['auto_download']}"
                    toast_time = time.time()
        elif ch in (ord('h'), ord('H')):
            toast_message = "Keys: [S]ync, [R]efresh, [P]laylist, [T]oggle Priority, [A]uto-DL, [Q]uit, [↑/↓] Navigate"
            toast_time = time.time()

# Start background sync worker thread on load
sync_thread = threading.Thread(target=background_sync_worker, daemon=True)
sync_thread.start()

# Initialize retro playback logs and master discover layout
retroactively_build_history(SD_PATH)
generate_playlist(SD_PATH, SD_MOUNT_PATH)
update_status_state()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--dashboard", "--cli", "-d"):
        run_dashboard()
    else:
        # threaded=True so status polling/navigation aren't blocked behind synchronous downloads
        app.run(debug=True, port=5000, threaded=True)