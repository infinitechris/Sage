import os
import json
import shutil
import calendar
import time
from email.utils import parsedate_to_datetime
from PIL import Image
from mutagen.mp3 import MP3

SD_MOUNT_PATH = "/run/media/triconda/170D-1A3D"
MOCK_SD_PATH = "/home/triconda/Projects/Sage/mock_sd"

current_sync_status = "Ready"

MAX_RECENT_ERRORS = 20
_recent_errors = []

def _log_error(message):
    """Records a backend error so it can be surfaced on the dashboard, in addition to printing it."""
    print(message)
    _recent_errors.insert(0, {"time": time.strftime("%H:%M:%S"), "message": message})
    del _recent_errors[MAX_RECENT_ERRORS:]

def get_recent_errors():
    return list(_recent_errors)

def clear_recent_errors():
    _recent_errors.clear()

def get_sync_status():
    return current_sync_status

def extract_pub_timestamp(entry_or_data):
    if isinstance(entry_or_data, dict):
        if entry_or_data.get("pub_timestamp"):
            try:
                return int(entry_or_data["pub_timestamp"])
            except (ValueError, TypeError):
                pass
        pub_str = entry_or_data.get("published") or entry_or_data.get("publish_date") or entry_or_data.get("pubDate") or entry_or_data.get("updated") or ""
    elif isinstance(entry_or_data, str):
        pub_str = entry_or_data
    else:
        return 0

    if not pub_str:
        return 0

    try:
        dt = parsedate_to_datetime(pub_str)
        return int(dt.timestamp())
    except Exception:
        pass

    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d"
    ):
        try:
            t_struct = time.strptime(pub_str, fmt)
            return int(calendar.timegm(t_struct))
        except Exception:
            continue

    return 0

def get_mp3_duration(file_path):
    try:
        audio = MP3(file_path)
        if audio.info and audio.info.length:
            return int(audio.info.length)
    except Exception as e:
        _log_error(f"Error reading mp3 duration for {file_path}: {e}")
    return 0

def process_podcast_art(feed_json_name, feed_folder_name, sd_podcast_folder):
    try:
        feed_json_path = os.path.join(MOCK_SD_PATH, "feeds", feed_json_name)
        if not os.path.exists(feed_json_path):
            return
            
        with open(feed_json_path, "r") as f:
            feed_data = json.load(f)
            
        image_filename = feed_data.get("image_file")
        if not image_filename:
            return
            
        source_image_path = os.path.join(MOCK_SD_PATH, "assets", image_filename)
        if not os.path.exists(source_image_path):
            return
            
        target_art_path = os.path.join(sd_podcast_folder, ".podcastart.jpg")
        
        # Resize and save as 80x80 JPEG
        with Image.open(source_image_path) as img:
            img = img.convert("RGB")
            img = img.resize((80, 80), Image.Resampling.LANCZOS)
            img.save(target_art_path, "JPEG", quality=90)
            
    except Exception as e:
        _log_error(f"Error processing podcast art for {feed_folder_name}: {e}")

def generate_playlist(mock_sd_path=MOCK_SD_PATH, sd_mount_path=SD_MOUNT_PATH):
    """
    Generates a master .m3u playlist at Podcasts/playlist.m3u (locally in mock_sd,
    working dir, and on mounted SD card).
    Includes only downloaded, active (unplayed and unarchived) episodes,
    ordered by publication timestamp (pub_timestamp DESC) with priority items first,
    referencing their local file paths.
    """
    local_feeds_dir = os.path.join(mock_sd_path, "feeds")
    local_downloads_dir = os.path.join(mock_sd_path, "downloads")
    
    eligible_episodes = []
    
    if os.path.exists(local_feeds_dir):
        for feed_file in sorted(os.listdir(local_feeds_dir)):
            if not feed_file.endswith(".json"):
                continue
            feed_path = os.path.join(local_feeds_dir, feed_file)
            try:
                with open(feed_path, "r") as f:
                    feed_data = json.load(f)
            except Exception as e:
                _log_error(f"Error reading feed {feed_file} for playlist: {e}")
                continue
                
            if not feed_data.get("active", True):
                continue
                
            feed_title = feed_data.get("feed_title", feed_file[:-5].replace("_", " "))
            feed_slug = feed_data.get("slug", feed_file[:-5])
            feed_priority = feed_data.get("priority", False)
            feed_folder = os.path.join(local_downloads_dir, feed_slug)
            
            for ep in feed_data.get("episodes", []):
                # Only active (unplayed and unarchived)
                if ep.get("played", False) or ep.get("archived", False):
                    continue
                
                ep_title = ep.get("title", "Unknown Title")
                safe_title = "".join(c for c in ep_title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
                
                # Check for physical file existence
                local_mp3 = None
                if os.path.exists(feed_folder):
                    standard_path = os.path.join(feed_folder, f"{safe_title}.mp3")
                    priority_path = os.path.join(feed_folder, f"PRIORITY_{safe_title}.mp3")
                    if os.path.exists(priority_path):
                        local_mp3 = priority_path
                    elif os.path.exists(standard_path):
                        local_mp3 = standard_path
                
                # Only include downloaded episodes
                if not local_mp3 and not ep.get("downloaded", False):
                    continue
                
                if not local_mp3:
                    # Fallback path if flagged downloaded
                    prefix = "PRIORITY_" if (feed_priority or ep.get("priority", False)) else ""
                    local_mp3 = os.path.join(feed_folder, f"{prefix}{safe_title}.mp3")
                
                pub_ts = ep.get("pub_timestamp") or extract_pub_timestamp(ep)
                is_priority = ep.get("priority", feed_priority)
                duration = get_mp3_duration(local_mp3) if (local_mp3 and os.path.exists(local_mp3)) else 0
                
                eligible_episodes.append({
                    "feed_title": feed_title,
                    "feed_slug": feed_slug,
                    "episode_title": ep_title,
                    "safe_title": safe_title,
                    "local_path": local_mp3,
                    "relative_sd_path": f"Podcasts/{feed_slug}/{os.path.basename(local_mp3)}",
                    "pub_timestamp": pub_ts,
                    "priority": is_priority,
                    "duration": duration
                })
                
    # Sort correctly by priority and pub_timestamp DESC (newest first)
    eligible_episodes.sort(key=lambda x: (not x["priority"], -x["pub_timestamp"]))
    
    # Target locations for playlist.m3u
    target_locations = [
        os.path.join(mock_sd_path, "Podcasts", "playlist.m3u"),
        os.path.join("Podcasts", "playlist.m3u")
    ]
    if sd_mount_path and os.path.exists(sd_mount_path):
        target_locations.append(os.path.join(sd_mount_path, "Podcasts", "playlist.m3u"))
        
    for target_file in target_locations:
        try:
            os.makedirs(os.path.dirname(target_file), exist_ok=True)
            with open(target_file, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for item in eligible_episodes:
                    duration_sec = item["duration"] if item["duration"] > 0 else -1
                    f.write(f"#EXTINF:{duration_sec},{item['feed_title']} - {item['episode_title']}\n")
                    # If target is on SD card, use SD relative path; otherwise use local path
                    if sd_mount_path and target_file.startswith(sd_mount_path):
                        f.write(f"{item['relative_sd_path']}\n")
                    else:
                        f.write(f"{item['local_path']}\n")
            print(f"Generated playlist at: {target_file} ({len(eligible_episodes)} tracks)")
        except Exception as e:
            _log_error(f"Error writing playlist to {target_file}: {e}")
            
    return len(eligible_episodes)

def purge_played_archived_downloads(mock_sd_path=MOCK_SD_PATH):
    """
    Removes locally downloaded audio files for episodes marked played or archived,
    clearing their 'downloaded' flag so they no longer appear as downloaded.
    Independent of SD mount/sync so it can run standalone (e.g. after a feed refresh).
    Returns the number of files purged.
    """
    local_downloads_dir = os.path.join(mock_sd_path, "downloads")
    local_feeds_dir = os.path.join(mock_sd_path, "feeds")
    purged_count = 0

    if not os.path.exists(local_feeds_dir):
        return purged_count

    for feed_json_name in os.listdir(local_feeds_dir):
        if not feed_json_name.endswith(".json"):
            continue
        feed_json_path = os.path.join(local_feeds_dir, feed_json_name)
        try:
            with open(feed_json_path, "r") as f:
                feed_data = json.load(f)
        except Exception as e:
            _log_error(f"Error reading feed {feed_json_name} during purge: {e}")
            continue

        slug = feed_data.get("slug", feed_json_name[:-5])
        feed_folder = os.path.join(local_downloads_dir, slug)
        if not os.path.isdir(feed_folder):
            continue

        changed = False
        for ep in feed_data.get("episodes", []):
            if not (ep.get("played", False) or ep.get("archived", False)):
                continue

            safe_title = "".join(c for c in ep.get("title", "") if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
            for candidate in (
                os.path.join(feed_folder, f"{safe_title}.mp3"),
                os.path.join(feed_folder, f"PRIORITY_{safe_title}.mp3")
            ):
                if os.path.exists(candidate):
                    try:
                        os.remove(candidate)
                        purged_count += 1
                        print(f"Purged played/archived episode: {candidate}")
                    except Exception as e:
                        _log_error(f"Error purging {candidate}: {e}")

            if ep.get("downloaded", False):
                ep["downloaded"] = False
                changed = True

        if changed:
            try:
                with open(feed_json_path, "w") as f:
                    json.dump(feed_data, f, indent=2)
            except Exception as e:
                _log_error(f"Error saving feed {feed_json_name} after purge: {e}")

        if os.path.isdir(feed_folder) and not os.listdir(feed_folder):
            try:
                os.rmdir(feed_folder)
            except Exception:
                pass

    return purged_count

def sync_to_sd():
    global current_sync_status
    
    sd_mounted = os.path.exists(SD_MOUNT_PATH)
    
    try:
        current_sync_status = "Reconciling local environment & flags..."
        state_file_path = os.path.join(SD_MOUNT_PATH, "state.json") if sd_mounted else ""
        sd_podcasts_dir = os.path.join(SD_MOUNT_PATH, "Podcasts") if sd_mounted else ""
        
        if sd_mounted:
            try:
                os.makedirs(sd_podcasts_dir, exist_ok=True)
            except OSError as e:
                _log_error(f"SD directory creation warning: {e}")
        
        # 1. Read existing state.json from real SD if available, otherwise empty dict
        device_states = {}
        if sd_mounted and os.path.exists(state_file_path):
            try:
                with open(state_file_path, "r") as f:
                    device_data = json.load(f)
                    for feed in device_data.get("feeds", []):
                        for item in feed.get("episodes", []):
                            device_states[item.get("file_path")] = {
                                "played": item.get("played", False),
                                "archived": item.get("archived", False),
                                "playback_position": item.get("playback_position", 0)
                            }
            except Exception as e:
                _log_error(f"Error reading device state.json: {e}")

        current_sync_status = "Processing Local Feeds and Metadata..."
        local_downloads_dir = os.path.join(MOCK_SD_PATH, "downloads")
        local_feeds_dir = os.path.join(MOCK_SD_PATH, "feeds")
        
        feed_episode_metadata = {}
        feed_priorities = {}
        master_feed_data_cache = {}
        
        if os.path.exists(local_feeds_dir):
            for feed_json_name in os.listdir(local_feeds_dir):
                if not feed_json_name.endswith(".json"):
                    continue
                feed_json_path = os.path.join(local_feeds_dir, feed_json_name)
                try:
                    with open(feed_json_path, "r") as f:
                        feed_data = json.load(f)
                        folder_key = feed_json_name[:-5]
                        feed_priorities[folder_key] = feed_data.get("priority", False)
                        feed_episode_metadata[folder_key] = {}
                        master_feed_data_cache[folder_key] = feed_data
                        for ep in feed_data.get("episodes", []):
                            ep_title = ep.get("title", "")
                            safe_title = "".join(c for c in ep_title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
                            feed_episode_metadata[folder_key][safe_title] = {
                                "publish_date": ep.get("published", ep.get("publish_date", "")),
                                "pub_timestamp": ep.get("pub_timestamp") or extract_pub_timestamp(ep),
                                "priority": ep.get("priority", feed_priorities[folder_key])
                            }
                except Exception as e:
                    _log_error(f"Error parsing feed json {feed_json_name}: {e}")

        # Build the master state structure first so we have the absolute source of truth
        current_sync_status = "Building Master State & Syncing Files..."
        master_state = {"feeds": []}
        valid_file_paths = set()
        played_or_archived_files = set()

        if os.path.exists(local_downloads_dir):
            for feed_folder_name in os.listdir(local_downloads_dir):
                feed_folder_path = os.path.join(local_downloads_dir, feed_folder_name)
                if not os.path.isdir(feed_folder_path):
                    continue
                    
                feed_title = feed_folder_name.replace('_', ' ')
                sd_podcast_folder = os.path.join(sd_podcasts_dir, feed_folder_name) if sd_mounted else ""
                
                if sd_mounted:
                    try:
                        os.makedirs(sd_podcast_folder, exist_ok=True)
                        process_podcast_art(f"{feed_folder_name}.json", feed_folder_name, sd_podcast_folder)
                    except Exception as e:
                        _log_error(f"Art processing skipped: {e}")
                
                is_priority = feed_priorities.get(feed_folder_name, False)
                feed_cache = master_feed_data_cache.get(feed_folder_name, {})
                
                processed_episodes = []
                for mp3_filename in os.listdir(feed_folder_path):
                    if not mp3_filename.endswith(".mp3"):
                        continue
                    
                    base_mp3_name = mp3_filename[9:] if mp3_filename.startswith("PRIORITY_") else mp3_filename
                    target_mp3_filename = f"PRIORITY_{base_mp3_name}" if is_priority else base_mp3_name
                    
                    local_mp3_path = os.path.join(feed_folder_path, mp3_filename)
                    sd_mp3_path = os.path.join(sd_podcast_folder, target_mp3_filename) if sd_mounted else ""
                    relative_file_path = f"Podcasts/{feed_folder_name}/{target_mp3_filename}"
                    
                    # Check if episode is played or archived in feed JSON or device states
                    ep_base_name = base_mp3_name[:-4]
                    is_played_or_archived = False
                    for ep in feed_cache.get("episodes", []):
                        safe_t = "".join(c for c in ep.get("title", "") if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
                        if safe_t == ep_base_name:
                            dev_flag = device_states.get(relative_file_path, {})
                            if ep.get("played", False) or ep.get("archived", False) or dev_flag.get("played", False) or dev_flag.get("archived", False):
                                is_played_or_archived = True
                            break

                    if is_played_or_archived:
                        played_or_archived_files.add(relative_file_path)
                        continue  # Skip adding to valid files so it gets cleaned up

                    valid_file_paths.add(relative_file_path)

                    if sd_mounted:
                        try:
                            if mp3_filename != target_mp3_filename:
                                old_sd_path = os.path.join(sd_podcast_folder, mp3_filename)
                                if os.path.exists(old_sd_path):
                                    os.remove(old_sd_path)
                                shutil.copy(local_mp3_path, sd_mp3_path)
                            
                            if os.path.exists(local_mp3_path):
                                if not os.path.exists(sd_mp3_path):
                                    shutil.copy(local_mp3_path, sd_mp3_path)
                                downloaded = True
                            else:
                                downloaded = False
                        except Exception as e:
                            _log_error(f"SD hardware write skipped: {e}")
                            downloaded = True
                    else:
                        downloaded = True

                    duration = get_mp3_duration(local_mp3_path)
                    meta_lookup = feed_episode_metadata.get(feed_folder_name, {}).get(ep_base_name, {})
                    publish_date = meta_lookup.get("publish_date", "")
                    pub_timestamp = meta_lookup.get("pub_timestamp", 0)
                    ep_priority = meta_lookup.get("priority", is_priority)

                    dev_state = device_states.get(relative_file_path, {})
                    processed_episodes.append({
                        "podcast_title": feed_title,
                        "episode_title": ep_base_name.replace('_', ' '),
                        "publish_date": publish_date,
                        "pub_timestamp": pub_timestamp,
                        "priority": ep_priority,
                        "duration": duration,
                        "file_path": relative_file_path,
                        "playback_position": dev_state.get("playback_position", 0),
                        "played": dev_state.get("played", False),
                        "archived": dev_state.get("archived", False),
                        "downloaded": downloaded
                    })
                
                if processed_episodes:
                    # Sort episodes by priority and pub_timestamp DESC
                    processed_episodes.sort(key=lambda ep: (not ep.get("priority", False), -ep.get("pub_timestamp", 0)))
                    master_state["feeds"].append({
                        "feed_title": feed_title,
                        "priority": is_priority,  # Persist priority status here!
                        "episodes": processed_episodes
                    })

        # Cleanup Phase: Rely strictly on valid_file_paths and played_or_archived_files derived from master state
        current_sync_status = "Cleaning storage based on master state..."
        
        # 1. Purge from physical SD if mounted
        if sd_mounted and os.path.exists(sd_podcasts_dir):
            for root, dirs, files in os.walk(sd_podcasts_dir):
                for file in files:
                    if file.endswith(".mp3"):
                        rel_folder = os.path.basename(root)
                        rel_path = f"Podcasts/{rel_folder}/{file}"
                        if rel_path not in valid_file_paths:
                            try:
                                os.remove(os.path.join(root, file))
                                print(f"Purged from physical SD: {rel_path}")
                            except Exception as e:
                                _log_error(f"Error removing file from SD: {e}")

        # 2. Purge locally from mock_sd/downloads
        if os.path.exists(local_downloads_dir):
            for feed_folder_name in os.listdir(local_downloads_dir):
                feed_folder_path = os.path.join(local_downloads_dir, feed_folder_name)
                if not os.path.isdir(feed_folder_path):
                    continue
                
                for mp3_filename in os.listdir(feed_folder_path):
                    if not mp3_filename.endswith(".mp3"):
                        continue
                    
                    is_priority = feed_priorities.get(feed_folder_name, False)
                    base_name = mp3_filename[9:] if mp3_filename.startswith("PRIORITY_") else mp3_filename
                    target_name = f"PRIORITY_{base_name}" if is_priority else base_name
                    rel_path = f"Podcasts/{feed_folder_name}/{target_name}"
                    
                    if rel_path not in valid_file_paths:
                        target_mp3 = os.path.join(feed_folder_path, mp3_filename)
                        try:
                            os.remove(target_mp3)
                            print(f"Purged local file: {target_mp3}")
                        except Exception as e:
                            _log_error(f"Error removing local file: {e}")
                
                if not os.listdir(feed_folder_path):
                    os.rmdir(feed_folder_path)

        # Write final state.json only if mounted
        if sd_mounted and sd_podcasts_dir:
            try:
                with open(state_file_path, "w") as f:
                    json.dump(master_state, f, indent=2)
            except Exception as e:
                _log_error(f"State file write skipped: {e}")
            
        # Generate the master .m3u playlist file
        current_sync_status = "Generating playlist..."
        generate_playlist(MOCK_SD_PATH, SD_MOUNT_PATH)

        current_sync_status = "Sync Complete!"
    except Exception as e:
        current_sync_status = f"Sync Failed: {str(e)}"
        _log_error(f"Sync to esPod failed: {e}")