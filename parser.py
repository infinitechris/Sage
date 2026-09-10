import feedparser
import json
import os
import requests
import calendar
import time
import tempfile
from email.utils import parsedate_to_datetime

def write_json_atomic(file_path, data, indent=2):
    """
    Writes data to a temporary file in the same directory, then renames it
    atomically using os.replace to prevent 0-byte or corrupted JSON files.
    """
    dir_name = os.path.dirname(file_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.replace(temp_path, file_path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e

def extract_pub_timestamp(entry_or_data):
    """
    Extracts a Unix epoch integer timestamp from a feedparser entry,
    dict with 'published'/'pub_timestamp', or raw date string.
    """
    if isinstance(entry_or_data, dict):
        if entry_or_data.get("pub_timestamp"):
            try:
                return int(entry_or_data["pub_timestamp"])
            except (ValueError, TypeError):
                pass
        if entry_or_data.get("published_parsed"):
            try:
                return int(calendar.timegm(entry_or_data["published_parsed"]))
            except Exception:
                pass
        pub_str = entry_or_data.get("published") or entry_or_data.get("pubDate") or entry_or_data.get("updated") or ""
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

def parse_feed(feed_url, output_dir="mock_sd"):
    parsed = feedparser.parse(feed_url)
    feed_title = parsed.feed.get('title', 'Unknown Feed')
    safe_name = "".join(c for c in feed_title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    
    file_path = os.path.join(output_dir, "feeds", f"{safe_name}.json")
    
    existing_episodes = {}
    auto_download_pref = True  # Default to True for new feeds
    filter_string = ""
    feed_priority = False
    auto_archive_days = 0  # 0 indicates Disabled
    
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                old_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupted/empty JSON on disk (e.g. from an interrupted write) -
            # fall back to defaults instead of crashing the whole refresh run.
            old_data = {}
        auto_download_pref = old_data.get("auto_download", True)
        filter_string = old_data.get("filter_string", "")
        feed_priority = old_data.get("priority", False)
        auto_archive_days = old_data.get("auto_archive_days", 0)
        for ep in old_data.get("episodes", []):
            existing_episodes[ep["title"]] = {
                "played": ep.get("played", False),
                "archived": ep.get("archived", False),
                "downloaded": ep.get("downloaded", False),
                "priority": ep.get("priority", feed_priority),
                "pub_timestamp": extract_pub_timestamp(ep)
            }
                
    # Compile active filter terms into a lowercase list
    filters = [f.strip().lower() for f in filter_string.split(",") if f.strip()]
                
    merged_episodes = []
    for entry in parsed.entries[:10]:
        audio_url = next((link['href'] for link in entry.get('links', []) if 'audio' in link.get('type', '')), None)
        title = entry.get('title', 'Unknown Title')
        title_lower = title.lower()
        
        # Check if this new episode matches any filter rules
        matches_filter = any(f in title_lower for f in filters)
        
        pub_ts = extract_pub_timestamp(entry)
        
        # Check if episode is older than the auto_archive threshold (skipped if 0 / Disabled)
        is_too_old = False
        if auto_archive_days > 0 and pub_ts > 0:
            age_seconds = time.time() - pub_ts
            if age_seconds > (auto_archive_days * 24 * 3600):
                is_too_old = True

        status = existing_episodes.get(title, {
            "played": False, 
            "archived": (matches_filter or is_too_old), # Auto-archive if matches filter or too old
            "downloaded": False if (matches_filter or is_too_old) else auto_download_pref,
            "priority": feed_priority,
            "pub_timestamp": pub_ts
        })
        
        # Guard: If an episode is already marked played, do NOT auto-archive it under any circumstances
        if status.get("played", False):
            is_too_old = False
            status["archived"] = False
            if not existing_episodes.get(title, {}).get("downloaded", False):
                status["downloaded"] = False

        # If it's already recorded, make sure filter/age enforcement applies if updated later
        if not status.get("played", False) and (matches_filter or is_too_old) and title not in existing_episodes:
            status["archived"] = True
            status["downloaded"] = False
            
            # Log auto-archival for newly added filter or age matching episodes
            from sync import add_playback_log
            reason_str = "filter matched" if matches_filter else f"older than {auto_archive_days}d limit"
            add_playback_log(feed_title, title, f"auto-archived ({reason_str})", "Sage Dashboard (Feed Parser)", mock_sd_path=output_dir)
        
        merged_episodes.append({
            "title": title,
            "published": entry.get('published', ''),
            "pub_timestamp": pub_ts if pub_ts else status.get("pub_timestamp", 0),
            "priority": status.get("priority", feed_priority),
            "audio_url": audio_url,
            "downloaded": status["downloaded"],
            "played": status["played"],
            "archived": status["archived"]
        })
        
    # Sort episodes by priority (priority first) and publication timestamp (newest first)
    merged_episodes.sort(key=lambda ep: (not ep.get("priority", False), -ep.get("pub_timestamp", 0)))

    # Handle artwork extraction and local caching
    image_url = parsed.feed.get('image', {}).get('href', '')
    if not image_url and 'itunes_image' in parsed.feed:
        image_url = parsed.feed.itunes_image.get('href', '')
        
    art_filename = f"{safe_name}.jpg"
    art_dir = os.path.join(output_dir, "assets")
    os.makedirs(art_dir, exist_ok=True)
    art_path = os.path.join(art_dir, art_filename)
    
    if image_url and not os.path.exists(art_path):
        try:
            img_data = requests.get(image_url, timeout=10).content
            with open(art_path, "wb") as img_file:
                img_file.write(img_data)
        except Exception:
            art_filename = "" # Fallback if download fails

    feed_data = {
        "feed_title": feed_title,
        "slug": safe_name,
        "feed_url": feed_url,
        "active": True,
        "priority": feed_priority,
        "auto_download": auto_download_pref,
        "auto_archive_days": auto_archive_days,
        "filter_string": filter_string,
        "image_file": art_filename if art_filename else f"{safe_name}.jpg",
        "episodes": merged_episodes
    }
    
    feeds_dir = os.path.join(output_dir, "feeds")
    os.makedirs(feeds_dir, exist_ok=True)
    write_json_atomic(file_path, feed_data, indent=2)
        
    return feed_data