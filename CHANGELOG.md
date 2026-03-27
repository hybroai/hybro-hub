# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.11] - 2026-03-26

### Fixed

- Lock file no longer truncated before flock acquisition: a blocked `hybro-hub start` call would silently erase the running daemon's PID, making `stop` and `status` report "not running" while the daemon was still alive

## [0.1.10] - 2026-03-26

### Added

- `agent start --config <file>`: load any adapter from a YAML config file; supports auto-discovery of `hybro-agent.yaml` / `.hybro-agent.yaml` in the current directory
- `agent_id` included in `agent_response` SSE events from the dispatcher

### Changed

- Hub startup reordered: agent discovery and sync now complete before relay registration, eliminating the "agent no longer available" race window on restart
- `agent start`: `adapter_type` argument is now optional (required only when not using `--config`)
- CLI `--port` override now detected via `ParameterSource.COMMANDLINE` instead of a hardcoded default comparison

### Fixed

- Agent card `name` field now reflects the value set in YAML config or `--name`; was incorrectly stripped before reaching the adapter constructor
- `TypeError` when YAML config included a `port` key: hub-level key `port` is now stripped before passing config to `load_adapter()`
- Frontend renders agent responses without `agent_id`; falls back to `agent_name` or `'Agent'` instead of silently dropping the message

## [0.1.9] - 2026-03-25

### Added

- `hub/lock.py`: extracted instance locking into a dedicated module
- `ruamel.yaml` dependency to preserve YAML comments and formatting on `save_api_key()`
- `HubConfig.hub_id` validator rejecting empty strings; enforce `load_config()` as the sole construction entry point
- `Dispatcher.cancel_task()` method to encapsulate cancel task HTTP call (removes direct `_get_client()` access from `HubDaemon`)
- Warn on unknown config keys in all models (catches typos such as `heartbeat_intervall`)
- Tests: comment preservation, friendly validation errors, unknown-key warnings, `api_key` coercion, `scan_range` tuple type, multi-part text extraction, interactive state, cancel task, and `hub_id` empty-string rejection

### Changed

- `CloudConfig.api_key` changed from `str = ""` to `str | None = None` to distinguish "not set" from an empty string
- `auto_discover_scan_range` type changed to `tuple[int, int]` for precision
- All config models marked `frozen=True`; `hub_id` is pre-resolved before `HubConfig` construction
- `HubConfig.heartbeat_interval` is now wired into `_heartbeat_loop` (was previously hardcoded)
- `load_config()` wraps `ValidationError` in a user-friendly `SystemExit` message
- Relay `register()` wrapped with targeted `httpx` error handlers in `_startup()` to surface 401/`ConnectError` as clear log messages instead of tracebacks
- Removed unused `PublishQueueConfig.max_retries_streaming` field
- Removed `PrivacyConfig.default_routing` and `RoutingPolicy` enum (no routing mechanism exists in the hub)

### Fixed

- `_extract_artifact_text`, `_status_text`, and `_message_text` now concatenate all text parts via `_collect_parts` instead of returning only the first part
- `load_config()`: explicit YAML `null` values now treated the same as absent keys, fixing `ValidationError` on startup when list fields were commented out in `config.yaml`
- `HubConfig` model validator coerces `None → []` for all `list[...]`-annotated fields, guarding against YAML null parsing
- `processing_status(input_required)` is now emitted alongside `task_interactive` in the `INTERACTIVE_STATES` branch, matching all other terminal branches
- Removed dead `else` clause from streaming refetch guard

## [0.1.8] - 2026-03-22

### Added

- Animated spinner on TTY for long-running operations in `agents`, `status`, and `stop` commands
- `agents`: live scanning animation, per-agent ✓/✗ health symbol, and found-N summary line
- `status`: ✓/✗ symbols for local daemon and cloud relay; animated dots during relay HTTP check
- `start`: prints confirmation and log path before Unix double-fork

### Changed

- One-shot commands (`agents`, `status`, `stop`) suppress `hub.*` INFO logs to keep output clean; daemon (`start`) retains full INFO logging
- `stop`: stale PID lock file is auto-removed; static "Stopping..." message on non-TTY, spinner on TTY
- `agent start`: cleaner output formatting

### Fixed

- `_detach_windows()` was accidentally embedded inside `_spinning_wait()` — restored as a top-level function
- `Callable` import moved to module level; removed string-quoted type annotation
- `_spinning_wait` now checks condition before sleeping, avoiding a full interval of unnecessary delay for fast operations
- Test fixtures: `mock_client.is_closed = False` added to prevent `httpx.AsyncClient` mock bypass in `test_agent_registry` and `test_dispatcher`
- CLI stop tests updated to mock `_spinning_wait` directly instead of patching `time`

## [0.1.7.1] - 2026-03-21

### Changed

- Updated project metadata

## [0.1.7] - 2026-03-21

### Added

- Background daemon for `hybro-hub start` (Unix double-fork, Windows detached re-launch); `--foreground` / `-f` keeps the process in the terminal
- `hybro-hub stop` — SIGTERM with timeout, then SIGKILL; removes `~/.hybro/hub.lock` when the daemon exits
- Exclusive instance lock and PID file at `~/.hybro/hub.lock` (fcntl on Unix, `msvcrt` on Windows)
- Rotating daemon logs at `~/.hybro/hub.log` (10 MB × 3 files)
- `hybro-hub status` reports local daemon state (running / stopped / stale PID) and clearer cloud relay errors (401 vs other HTTP vs unreachable)
- CLI tests for `status`, plus coverage for lock, start, and stop paths

### Changed

- `load_config` logs successful file loads at DEBUG instead of INFO for quieter user-facing CLI output

## [0.1.6] - 2026-03-20

### Changed

- Show active/inactive agent breakdown in `hybro-hub status` (total, active, and inactive counts displayed separately)



## [0.1.5] - 2026-03-17

### Changed

- Make `a2a-adapter` a core dependency (no longer requires separate install)
- License changed from MIT to Apache 2.0
- Emit `artifact_update` instead of `agent_token` for streaming

### Fixed

- Prevent resource leaks in dispatcher, registry, relay, and queue

## [0.1.4] - 2026-03-13

### Fixed

- hybro-hub should error out if a wrong model name is provided

## [0.1.3] - 2026-03-13

### Added

- GitHub Actions workflow for automated PyPI publishing via trusted publishers

## [0.1.2] - 2026-03-12

### Added

- Initial release of hybro-hub
- Hub daemon for bridging local A2A agents to hybro.ai relay
- Python client for Hybro Gateway API
- Agent registry for managing local agents
- Relay client for cloud communication
- CLI interface (`hybro-hub` command)
- OpenClaw and n8n CLI support
- Disk-backed publish queue for reliability
- API key auth for publish, SSE read timeout

### Fixed

- Improved graceful shutdown and task tracking
- Tighten port enumeration correctness and thread safety
- Replace psutil port enumeration with unprivileged OS-native strategies
- Forward all relay event types to daemon
- Stabilize agent identity across restarts
- Preserve FIFO retry queue
- Improve cross-platform support
