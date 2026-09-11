# Sage Roadmap

This roadmap records intended direction rather than a fixed release schedule.
Credit estimates are rough implementation ranges and include focused validation,
but they may change as hardware behavior and migration requirements become clearer.

## Version 2

Version 2 focuses on making Sage portable beyond its original development machine
and making playback state durable enough to resume reliably across esPod restarts
and SD-card syncs.

### First-Run Setup and Settings

**Estimated total:** 900-1,300 credits

| Feature | Estimated credits |
| --- | ---: |
| Persistent config and runtime path refactor | 180-260 |
| First-run setup flow | 180-260 |
| Removable-drive discovery and path testing | 180-260 |
| Settings UI, migration flow, and worker reconfiguration | 240-340 |
| Validation, documentation, and packaging checks | 100-160 |

- Replace username-specific local and device paths with persistent configuration.
- Show a first-run setup flow before starting background jobs or writing files.
- Configure the local podcast library and esPod SD-card mount independently.
- Discover likely removable drives and show filesystem, capacity, and mount details.
- Provide a `Test paths` action with readable, writable, and expected-layout checks.
- Allow both paths to be changed later from a dedicated Settings screen.
- Offer an explicit local-library migration workflow rather than silently moving data.
- Keep Sage usable when the configured device is disconnected.
- Support environment-variable overrides for headless or packaged deployments.
- Remove import-time path capture from function defaults and restart or safely
  reconfigure workers after settings change.

**Done when:** a new user can clone Sage, launch it without editing Python files,
select or create a local library, select an esPod volume, validate both locations,
and later change either path without stale workers continuing to use the old value.

### Durable Current Playback State

**Estimated total:** 900-1,400 credits

| Feature | Estimated credits |
| --- | ---: |
| Versioned state schema and stable episode IDs | 150-220 |
| Periodic, event-driven, and recoverable checkpoints | 200-300 |
| Restart restoration and seek resumption | 150-220 |
| On-device Played and Archived actions | 200-300 |
| Sage playback display and state reconciliation | 100-180 |
| Shared contract fixtures and documentation | 120-180 |

- Persist the current episode path and playback position on pause.
- Checkpoint active playback approximately once per minute.
- Persist before changing tracks, marking an episode played, or shutting down cleanly.
- Restore the episode and seek position after an esPod restart.
- Preserve the existing rule that playback beyond 89% counts as played.
- Add explicit `Mark Played` and `Archive` actions on esPod from both Now Playing
  and Queue views.
- Confirm destructive actions or provide an immediate undo path, especially for the
  currently playing episode.
- Remove a manually played or archived episode from the active device queue and
  advance cleanly when it was the current episode.
- Persist manual actions immediately to `state.json`, while deferring physical audio
  deletion to the next Sage sync so accidental actions remain recoverable.
- Show clear on-device success or failure feedback instead of silently changing state.
- Make state writes atomic or recoverable so power loss cannot corrupt `state.json`.
- Avoid unnecessary SD-card writes by checkpointing only after meaningful progress.
- Add a state schema version and define conflict behavior when Sage and esPod both
  changed state since the previous sync.
- Show the last reported episode, position, and checkpoint time in Sage without
  presenting disconnected data as live playback.

**Done when:** pausing, restarting esPod, or syncing the card resumes the same episode
near its last checkpoint; natural completion and playback beyond 89% still mark it
played and advance the queue; manual Played and Archived actions survive restart and
reconcile into Sage; interrupted writes leave a recoverable state file.

### Supporting Contract Work

- Update both copies of `docs/SD_MANIFEST_SPEC.md` when the playback-state schema is
  finalized.
- Add stable episode identifiers so state survives filename or priority-prefix changes.
- Add fixture-based contract tests shared by Sage and esPod for playlist ordering,
  state merging, resume positions, completion, and malformed-file recovery.

## Later Candidates

These are valuable but should follow the v2 configuration and state foundations.

### TinyUSB Mass-Storage Foundation

**Estimated effort:** 1,200-2,000 credits

Establish reliable USB Mass Storage Class access to the esPod SD card before treating
USB as a supported sync transport. The firmware must transfer exclusive filesystem
ownership to the host while USB storage is active: stop playback, flush and close open
files, unmount or suspend firmware SD access, expose the block device through TinyUSB,
then remount and rebuild device state only after the host ejects it cleanly. Sage must
detect the exported volume without relying on a username-specific mount path.

Validate repeated connect, copy, eject, reconnect, reboot, cable removal during an
active transfer, full-card handling, and malformed or interrupted state writes on the
actual Cardputer Adv hardware. Do not permit simultaneous firmware and host writes to
the FAT filesystem.

**Done when:** the device enumerates consistently on supported hosts, Sage can update
podcast files and manifests through USB across repeated cycles, esPod resumes normal SD
access after safe eject, and interruption testing does not corrupt the filesystem or
silently lose playback state.

### Transactional Sync and Recovery

**Estimated effort:** 900-1,500 credits

Use a staged manifest, checksums, free-space checks, and a resumable copy journal so an
unplugged card or interrupted TinyUSB session cannot leave a half-synchronized queue.
Include backup and repair tools for configuration, feed metadata, playback history,
and device state. This milestone depends on reliable TinyUSB ownership handoff first.

### Wireless Device Sync

**Estimated effort:** 1,800-3,000+ credits

Add authenticated local-network discovery and transfer so Sage can exchange manifests,
playback state, and selected audio without removing the SD card. This requires protocol
design, interruption recovery, and firmware networking. Begin only after TinyUSB and
transactional sync provide a proven local transport and recovery baseline.

### Queue Editing and Conflict Resolution

**Estimated effort:** 900-1,500 credits

Allow reorder, remove, pin-next, and per-device queue changes from Sage. Stable episode
IDs and explicit conflict rules are prerequisites so a device update cannot silently
undo a dashboard edit.

### SQLite Library and Event Log

**Estimated effort:** 1,200-2,200 credits

Move mutable feed, episode, and playback metadata from many JSON files into SQLite with
migrations and an append-only playback event log. Keep generated JSON and M3U files as
device-facing artifacts. This becomes worthwhile as history, multiple devices, and
concurrent background work grow.

### Installable Desktop Service

**Estimated effort:** 900-1,600 credits

Package Sage with a supported launcher, background service, log rotation, dependency
management, and upgrade flow for Linux first. This removes virtual-environment and
terminal setup from normal use.

### Download Resilience and Storage Policy

**Estimated effort:** 700-1,200 credits

Add resumable downloads, content-length and audio validation, retry backoff, storage
budgets, and per-feed retention by episode count as well as age. Surface failed and
partial downloads directly in the dashboard.

## Suggested Order

1. First-run setup and Settings.
2. Durable current playback state and schema versioning.
3. Shared contract fixtures and stable episode IDs.
4. TinyUSB mass-storage foundation and hardware reliability testing.
5. Transactional sync and recovery over the proven USB transport.
6. Queue editing.
7. Wireless sync, reusing the transactional protocol and recovery rules.
8. SQLite migration and desktop packaging when maintenance cost justifies them.
