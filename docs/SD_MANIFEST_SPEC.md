# SD Card Manifest Spec

This is the contract between the **Sage** companion dashboard (Python/Flask,
PC-side) and the **esPod** firmware (C++/PlatformIO, Cardputer-side). Sage
writes these files to the SD card; esPod reads them to render its UI and
writes playback state back for Sage to reconcile on the next sync.

This file is mirrored in both repos (`Sage/docs/SD_MANIFEST_SPEC.md` and
`esPod/docs/SD_MANIFEST_SPEC.md`). **Update both copies together** when the
format changes.

## On-device directory layout

```
/                              (SD card root)
├── state.json                 # master playback/queue state (see below)
└── Podcasts/
    ├── playlist.m3u           # master queue, priority shows first
    └── <Feed_Slug>/
        ├── .podcastart.jpg    # 80x80 cover art
        ├── <Episode_Title>.mp3
        └── PRIORITY_<Episode_Title>.mp3   # priority episodes get this prefix instead
```

- `<Feed_Slug>` and `<Episode_Title>` are sanitized: only alphanumerics, spaces,
  underscores and hyphens are kept, then spaces are replaced with `_`.
- An episode file is prefixed with `PRIORITY_` instead of having a separate
  field lookup — the filename itself encodes priority so the firmware doesn't
  need to parse `state.json` just to sort the file list.
- Only downloaded, non-played, non-archived episodes are ever written to the
  device. Everything else is purged from the SD card during sync.

## `state.json`

```jsonc
{
  "feeds": [
    {
      "feed_title": "Hard Fork",
      "priority": false,
      "episodes": [
        {
          "podcast_title": "Hard Fork",
          "episode_title": "The A.I. Mob That Attacked Hugging Face",
          "publish_date": "Fri, 4 Sep 2026 11:00:00 +0000",
          "pub_timestamp": 1788519600,
          "priority": false,
          "duration": 2734,                     // seconds, read from ID3/MP3 header
          "file_path": "Podcasts/Hard_Fork/The_AI_Mob_That_Attacked_Hugging_Face.mp3",
          "playback_position": 0,                // seconds; esPod owns this field
          "played": false,                       // esPod owns this field
          "archived": false,                     // esPod owns this field
          "downloaded": true
        }
      ]
    }
  ]
}
```

**Ownership**: Sage regenerates this file wholesale on every sync (`sync_to_sd()`
in `sync.py`). It reads the *previous* `state.json` off the device first so
`played` / `archived` / `playback_position` values esPod has written since the
last sync are preserved (keyed by `file_path`) before being merged back into
the fresh copy.

**esPod's responsibility**: update `played`, `archived`, and
`playback_position` for the episode currently/previously playing, and persist
those edits back into `state.json` on disconnect/unmount. Do not rename or
remove fields you don't use — Sage will silently default anything missing
(`played`/`archived` default to `false`, `playback_position` defaults to `0`).

## `Podcasts/playlist.m3u`

Standard extended M3U, ordered priority-first then newest-first:

```
#EXTM3U
#EXTINF:2734,Hard Fork - The A.I. Mob That Attacked Hugging Face
Podcasts/Hard_Fork/The_AI_Mob_That_Attacked_Hugging_Face.mp3
```

- Duration is in seconds; `-1` means unknown (mirrors standard M3U convention).
- The path on the second line is always the on-device relative path
  (`Podcasts/<slug>/<file>.mp3`), safe to resolve directly against the SD
  card root.

## What's *not* part of this contract

Sage's `mock_sd/feeds/<Feed>.json` files (RSS metadata, `feed_url`,
`auto_download`, `filter_string`, etc.) are internal to the dashboard and are
**never copied to the device**. esPod should never need to read them — if a
firmware feature seems to require it, that's a sign the field belongs in
`state.json` instead, and this spec should be updated accordingly.
