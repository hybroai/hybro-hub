# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
