# MQTT Gateway for SANlight Mesh 0.4.1

This maintenance release adds diagnostics and a controlled manual recovery path
for a failure mode in which BlueZ accepts read-only Mesh transmissions but none
of the selected lamps returns a matching status. It does not claim to eliminate
the underlying Bluetooth transport fault and does not add automatic recovery.

## Highlights

- Classify complete read-only Mesh silence as `mesh-no-response`, while keeping
  partial and ordinary per-lamp failures distinct.
- Persist and publish the last verified Mesh response, consecutive complete
  no-response command count and last affected command in optional gateway health
  information.
- Add `sanlight-gateway capture-mesh-failure NODE` to preserve the current Mesh
  daemon state while collecting before/after UART counters, kernel and
  Mesh-daemon journals, a read-only `get-live` probe and an HCI `btmon` capture.
- Add `sanlight-gateway recover-mesh` for the validated sequence that stops the
  MQTT gateway, restarts the local BlueZ Mesh daemon, waits for its D-Bus
  interface and starts the gateway again.
- Extend `sanlight-gateway doctor` and `collect-diagnostics` with Raspberry Pi
  model, HCI path, PL011 UART overrun counts, recent kernel Bluetooth errors and
  persisted Mesh-response health.

## Compatibility

The MQTT topic contract remains API v1. Version 0.4.1 adds the
`mesh-no-response` result status and an optional `meshHealth` section to retained
gateway information. Existing clients that ignore unknown optional fields remain
compatible. No configuration migration is required, and the companion
`ioBroker.sanlightmesh` adapter does not require a version update for this
maintenance release.

The new management commands are read-only with respect to the lamps. Recovery
restarts local services but does not send a lamp write command. Existing daylight
reading, MaxBrightness control, clock operations, blackout protection, queueing,
rate limits and sequence-state safety remain unchanged.

## Hardware validation

The branch was validated on a Raspberry Pi 3 Model B with Debian 13, BlueZ 5.82
and two real SANlight lamps. The final offline suite contains 171 tests.

A healthy diagnostic capture:

- completed its read-only `get-live` probe successfully;
- produced both text and binary HCI captures;
- restored the MQTT gateway service;
- recorded no new kernel Bluetooth error during the probe; and
- kept the cumulative PL011 UART overrun counter unchanged at 42.

The manual recovery command was then exercised in the healthy state. The gateway
startup refresh and a subsequent all-lamp daylight read both completed as
verified, and the companion ioBroker adapter again reported two verified lamps,
no command errors and a consistent 18:6 daylight summary.

## Important limitations

- The observed PL011 overruns and kernel `Frame reassembly failed` messages are
  evidence of Bluetooth transport instability, but they do not yet prove the
  root cause of the original outage.
- `mesh-no-response` can also occur when every selected lamp is powered off or
  unreachable. For that reason, this release deliberately does not restart the
  Mesh daemon automatically.
- Run `capture-mesh-failure` before recovery when the failure is active. Restarting
  the Mesh daemon first destroys the transport state needed for comparison.
- Review diagnostic bundles before sharing them. `hci.btsnoop` contains raw
  Bluetooth traffic, and reports can include hostnames, Bluetooth addresses and
  local filesystem paths even when cryptographic keys and passwords are redacted.

## Installation

Use the release archive attached to the GitHub release, verify its SHA-256 file,
extract it into a new directory, run the offline tests, and reinstall while
reusing the existing configuration and protected Mesh state. Never restore an
older Bluetooth Mesh sequence-state directory during an update or rollback.
