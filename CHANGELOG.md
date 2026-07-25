# Changelog

All notable changes to this community project are documented here. The project is pre-1.0; release notes must identify configuration or protocol compatibility changes explicitly.

## Unreleased

- Classify complete read-only Mesh silence as `mesh-no-response` when BlueZ accepts the requests but no selected lamp returns a matching status, while preserving ordinary partial and per-lamp failures.
- Persist the last successful Mesh response and consecutive complete no-response command count in non-secret gateway state and publish the optional health summary in retained gateway information.
- Add `sanlight-gateway capture-mesh-failure` for a read-only `get-live` probe with before/after UART counters, kernel and Mesh-daemon journals, and an HCI `btmon` capture without restarting the Mesh daemon.
- Add `sanlight-gateway recover-mesh` for the validated stop-gateway, restart-Mesh, wait-for-D-Bus, start-gateway recovery sequence.
- Extend doctor and redacted diagnostics with Raspberry Pi Bluetooth transport information, cumulative PL011 overrun counts and recent kernel HCI errors.

## 0.4.0 - 2026-07-19

- Add a dedicated read-only `read-daylight` MQTT action and `get-daylight` CLI command for one lamp or all detected lamps without extending normal refresh.
- Query combined daylight data first and fall back once to the configuration-only read, with strict source, destination, AppKey and request/status-opcode correlation. Hardware validation confirms the combined response contains lamp time, live brightness, MaxBrightness and the complete daylight configuration.
- Preserve complete raw daylight status PDUs, expose conservatively parsed configuration IDs, names and ordered time/brightness values, and keep the last verified configuration when a later read fails or has an unknown layout.
- Publish optional retained `daylightConfiguration` node state and structured per-node command results while deliberately leaving photoperiod and farm-policy interpretation to MQTT clients.
- Hardware-validate repeated direct reads and MQTT all-lamp reads with matching 12:12 profiles, mixed 18:6/12:12 profiles and two-point all-dark profiles; confirm verified retained state and no daylight write operation.
- Add offline protocol, executor, persistence, service, schema and command-validation coverage for the daylight reader, including real captured 12:12, 18:6 and all-dark payload fixtures.

## 0.3.0 - 2026-07-18

- Add explicit MQTT clock controls for one lamp or all lamps: `sync-clock`, `set-clock` and the Mesh-free `refresh-gateway-info` action.
- Replace MQTT `lampTimeMs` with `lampClockSeconds` (`0..86399`) and second-resolution `lampClock`; keep millisecond handling internal to the vendor protocol implementation.
- Publish the gateway local-clock snapshot in retained `gateway/info`, verify every clock write by live readback, and report partial per-lamp outcomes.
- Hardware-validate per-lamp and all-lamp refresh, synchronization and arbitrary clock targets on two lamps, including input rejection, sequential verification and one-shot command reset.
- Confirm that both validated lamp clocks restart at `00:00:00` after power is restored and can be recovered with one verified all-lamp synchronization.
- Clarify refresh and all-lamp result messages so they describe clock data and plural targets accurately.

## 0.2.0 - 2026-07-17

- Make GitHub Actions invoke repository shell scripts through `bash`, so CI does not depend on executable bits preserved by the checkout platform.
- Verify the generated release checksum and rerun the complete offline safety suite from the extracted release archive.
- Restore explicit release-archive exclusions for the ioBroker MQTT password file and the managed Mosquitto password database.
- Add an offline GitHub Actions regression gate for Python 3.11 and 3.13, shell syntax and secret-free release archives.
- Replace the oversized first-line operations document with a short operator guide while preserving the complete technical reference under `docs/ADVANCED_REFERENCE.md`.
- Add explicit release metadata and first-public-release notes.
- Use neutral gateway wording in the installed doctor output.
- Prevent root-run doctor and diagnostic repository checks from refreshing or rewriting the Git index by disabling Git optional locks for all read-only repository inspection.
- Extend read-only MQTT node state with lamp time and current effective-output data from `GetUptimeAndBrightness`; preserve the raw uint16 value, expose the empirical `raw / 10` percentage estimate separately, and keep MaxBrightness as the independent configured schedule scaling limit.
- Standardize public documentation and service descriptions so the independent gateway is not presented as an official SANlight product; retain SANlight names only for compatibility and protocol references.
- Make IV Index installation failures actionable: clarify that normal installs require no manual value, document trusted sources and exact recovery steps, and improve the CLI error without changing identity selection or Mesh state behavior.
- Clarify the SANlight app App-ID, CDB provisioner identity, Mesh source address, AppKey index and AID terminology without changing gateway behavior.
- Simplify the public README and first-time setup path, remove duplicated adapter configuration, and clarify the gateway-to-adapter handoff.
- Add the MIT license used by the companion adapter repository.
- Rename the repository to `sanlight-mesh-mqtt-gateway`.
- Define the two-repository architecture with `ioBroker.sanlightmesh` as the companion adapter.
- Add the interactive gateway configuration and installation wrapper.
- Add the `sanlight-gateway` health, log and redacted-diagnostics helper.
- Add secret-free tagged release archive tooling without introducing Debian packaging.
- Add architecture, installer, release, security and AI-assisted support documentation.
- Add and strengthen MQTT v1 JSON schemas, including node metadata.

## 0.1.1 - 2026-07-15

- Hardware-validate the always-on MQTT gateway with two SANlight nodes.
- Harden retained-command handling using MQTT 5 subscription options.
- Validate deduplication, expiry, coalescing, rate limiting, blackout/restore, broker restart, gateway restart and full host reboot recovery.
- Enable unbuffered systemd journal logging.

## 0.1.0 - 2026-07-15

- Add the first MQTT API v1 gateway implementation and systemd service.
