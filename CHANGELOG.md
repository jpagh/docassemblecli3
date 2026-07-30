<!-- markdownlint-configure-file {"MD024": {"siblings_only": true}} -->

# Changelog

All notable changes to this project will be documented in this file.

This changelog was reconstructed from the repository's git tags and commit
history on 2026-04-10. Early releases, especially before 0.2.1, include
summaries inferred from diffs where commit messages were not descriptive.

## Unreleased

## [26.7.1] - 2026-07-30

### Changed

- Performance improvements to the `watch` command's file monitoring. Saving a
  file repeatedly in quick succession (for example with auto-save or
  format-on-save) is now handled more efficiently, and fewer files are
  re-examined when nothing meaningful has changed.

### Fixed

- Improved error handling for invalid project configuration files.
- `da download` and `da uninstall` now use the same proxy-safe connections as
  other commands, avoiding possible hangs behind certain HTTP/3 reverse proxies.
- `da download` now works reliably on Windows.
- `da config new` no longer silently overwrites an existing config file.
- `da install` now matches `watch` and only restarts the server when Python
  files inside the `docassemble/` package directory change, so editing test files
  no longer triggers an unnecessary restart.
- The `watch` command now detects more rapid file changes, including edits that
  keep the same file size.

## [26.7.0] - 2026-07-11

### Fixed

- No longer restarts the server when Python files outside of the docassemble
  directory change (specifically to avoid tests causing restarts).

## [26.4.2] - 2026-04-10

### Fixed

- Fixed Playground installs and `watch` startup installs hanging behind some
  HTTP/3 reverse proxies by using fresh `niquests` sessions with HTTP/3 disabled
  for CLI HTTP requests.
- Fixed Playground project existence checks to read the API's JSON response
  instead of testing membership on the raw HTTP response object.

## [26.4.1] - 2026-04-04

### Added

- Added dry-run install previews so uploads can be inspected before sending any
  files.
- Expanded project configuration support with additional commands and options.
- Added server cleanup handling in command resolution.
- Server listings now include each server's `apiurl`.

### Changed

- Replaced `requests` with `niquests` for HTTP calls.

### Fixed

- Corrected version formatting and bumpversion/Taplo configuration after the
  CalVer transition.
- Reformatted the README.

## [26.04.0] - 2026-04-03

### Added

- Added package-local project configuration handling for `install` and `watch`.

### Changed

- Improved command resolution so project configuration can override or augment
  the selected config file.

## [26.03.1] - 2026-03-25

### Added

- Added `.dawatchignore` support and broader ignore-pattern handling for
  `watch`.
- Added shared directory/playground CLI parameters with test coverage.
- Added license normalization when generating package metadata.
- Added pre-commit hooks for Ruff and Taplo, plus initial Taplo formatting
  rules.

### Changed

- Switched packaging to `setuptools` and modernized the publishing workflow.
- Reorganized `pyproject.toml`, updated dependencies, raised the minimum Python
  version to 3.12, and changed versioning to CalVer.
- Pulled in upstream parity features while modernizing packaging and workflow.

### Fixed

- Resolved a Python 3.14 warning.
- Improved `watch` shutdown behavior on keyboard interrupt.
- Cleaned up README installation and usage instructions.
- Removed unused pytest configuration.
- Updated `.gitignore` coverage for additional generated files.
- Corrected the GitHub Actions PyPI publishing workflow.

## [0.5.1] - 2026-03-19

### Fixed

- Normalized installation paths before matching `.gitignore` rules.
- Switched `adjusted_root` to `os.path.relpath`, improving package installer
  behavior across platforms and directory layouts.

## [0.5.0] - 2025-07-18

### Added

- Added optional bell sound notifications.

## [0.4.0] - 2025-06-08

### Added

- Added progress and elapsed-time output while waiting for the server to finish
  installing a package.

### Fixed

- Removed the pinned version constraint from the GitHub Action used for PyPI
  publishing.

## [0.3.7] - 2025-02-14

### Fixed

- Broadened exception handling in checksum calculation used by file watching.

## [0.3.6] - 2025-02-12

### Added

- Expanded `display_servers` and `watch` output to include playground,
  directory, and startup configuration information.

### Changed

- Updated the README to clarify how server, directory, and playground
  configuration interact.

## [0.3.5] - 2025-02-12

### Fixed

- Renamed the checksum helper and handled `PermissionError` during file
  scanning.

## [0.3.4] - 2025-02-06

### Added

- Added directory and playground support to server selection and environment
  update helpers.

## [0.3.3] - 2025-02-05

### Added

- Centralized excluded-directory handling for both `watch` and `install`.
- Enhanced server selection to accept additional parameters and provide better
  directory scanning feedback.

### Changed

- Updated the README to reflect current user paths and `watch` behavior.

## [0.3.2] - 2025-02-05

### Added

- Added progress messages during directory scanning.

## [0.3.1] - 2025-02-05

### Changed

- Removed the CLI config `scan-directory` setting.

## [0.3.0] - 2025-02-05

### Added

- Improved directory scanning and `.gitignore` handling.

### Changed

- Updated project dependencies and applied Ruff formatting.
- Updated the README's documented Python version requirement.

## [0.2.3] - 2025-02-05

### Changed

- Release housekeeping only; no additional code changes were recorded between
  0.2.2 and 0.2.3.

## [0.2.2] - 2025-01-21

### Changed

- Moved project version management into `pyproject.toml`.

### Fixed

- Corrected the package name used for version checks.

## [0.2.1] - 2024-12-07

### Added

- Added `da config test` to validate a configured server URL and API key.

### Fixed

- Added a safety net for file-not-found errors.

## [0.2.0] - 2024-09-30

### Added

- Added checksum-based directory scanning to seed watch state before file events
  arrive.
- Expanded the default ignore list to cover additional temporary and editor lock
  files.

### Changed

- Updated `watch` to compare file checksums so only real file-content changes
  trigger installs and restart decisions.

## [0.1.0] - 2024-09-11

### Added

- Made `watch` honor the package's `.gitignore`, with a fallback to the default
  generated ignore list.

### Changed

- Clarified that the `--buffer` option only applies when a restart wait is
  needed.

## [0.0.5] - 2024-08-21

### Added

- `create` now generates a default `.gitignore` file.

### Changed

- Synced several behaviors with upstream `docassemblecli`, including package
  version handling, longer install timeouts, and ignore-process handling.
- Refactored manual waiting for background restart processes.

### Fixed

- Improved failed-request and general error handling.

## [0.0.4] - 2024-08-19

### Fixed

- Fixed a regression from 0.0.3 that raised an exception when the server did not
  need to restart.
- Corrected GitHub Actions configuration for trusted PyPI publishing.

## [0.0.3] - 2024-08-19

### Changed

- Stopped manually waiting for background restart processes on docassemble
  servers 1.5.3 and newer.
- Updated the Python publishing workflow.

## [0.0.2] - 2024-08-17

### Added

- First tagged release of `docassemblecli3`.
- Added a `--version` option and support for running the package with
  `python -m docassemblecli3`.
- Added initial GitHub Actions publishing workflow support.

### Changed

- Iterated on project metadata, entry points, and the build backend during the
  first packaging setup.
