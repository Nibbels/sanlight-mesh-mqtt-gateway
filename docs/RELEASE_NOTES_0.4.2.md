# MQTT Gateway for SANlight Mesh 0.4.2

This maintenance release adds conservative automatic recovery for the recurring
BlueZ Mesh TX-stall failure mode that was diagnosed in 0.4.1. The watchdog is
designed to fail closed: it restarts the local Mesh transport only after the
captured failure signature has been confirmed by an independent read-only probe.

## Highlights

- Add `sanlight-mesh-watchdog.timer`, running once per minute but remaining silent
  and sending no Mesh traffic while no eligible incident exists.
- Require a recent complete all-node read-only `mesh-no-response` incident and a
  previous verified lamp response before an active confirmation probe is allowed.
- Stop only the MQTT gateway worker for the isolated probe, leaving the BlueZ Mesh
  daemon in its current state so the suspected transport failure can be tested.
- Confirm the captured TX-stall signature only when BlueZ accepts the probe, no
  matching lamp status arrives and the PL011 UART TX counter changes by exactly
  zero bytes.
- Recover through the existing validated `sanlight-gateway recover-mesh` sequence
  and require a new verified read-only lamp response afterwards.
- Add cooldown and rolling-window limits that prevent restart loops.
- Allow the hardened watchdog systemd unit to use the `AF_ALG` crypto socket family
  required by the BlueZ Mesh probe path.

## Safety model

The watchdog never replays the application command that originally failed and
never retries a lamp write command. The confirmation probe itself is read-only.

Automatic recovery is deliberately rejected when any of the following is true:

- the failure affected only an individual lamp;
- the failed command was not a supported read-only action;
- there is no sufficiently recent earlier verified Mesh response;
- the incident is too old;
- the probe cooldown or recovery cooldown is active;
- BlueZ does not clearly accept the isolated probe;
- a lamp response is observed;
- UART counters are unavailable or ambiguous; or
- UART TX increases, which indicates that the controller actually transmitted.

The default loop protection permits one active probe per hour, enforces a
30-minute recovery cooldown and permits at most two automatic recoveries in a
six-hour rolling window. `recoveryHistory` in the private watchdog state is this
rolling safety window, not a permanent audit history; long-term events remain in
the systemd journal.

## Compatibility

The MQTT topic contract remains API v1. No MQTT schema migration is required and
existing ioBroker clients remain compatible.

Existing gateway configurations enable the watchdog with conservative defaults;
an optional `[watchdog]` section can override the documented limits or disable the
watchdog. The timer can also be disabled through systemd when automatic recovery
is not desired.

This release changes local operational behavior only in the narrowly confirmed
TX-stall case: the watchdog may automatically restart the local BlueZ Mesh
transport and MQTT gateway worker. It does not modify lamp schedules, brightness,
clocks or any other lamp configuration during detection or recovery.

## Hardware and field validation

The watchdog implementation was developed and validated on the Raspberry Pi 3
SANlight gateway running Debian 13 and BlueZ 5.82 with two real SANlight lamps.
The implementation commit was validated by the complete offline suite on Python
3.11 and 3.13 with 184 tests, shell syntax checks, static output scanning and the
release-archive validation path.

The initial controlled hardware test reproduced the captured stall signature and
verified that the watchdog restored communication automatically.

A later natural production incident on 2026-08-07 provided the first unattended
field validation:

- an eligible complete all-node read failure was detected;
- the isolated probe against node `0002` was accepted by BlueZ;
- no lamp response arrived;
- PL011 UART TX remained `4262388 -> 4262388` (`delta = 0`);
- the watchdog classified the event as `confirmed-tx-stall`;
- the local Mesh transport was restarted automatically; and
- a new verified read-only lamp response arrived about six seconds after recovery
  began, without operator intervention.

This confirms that the previously observed BlueZ Mesh TX stall can still occur in
normal operation, while the new watchdog can detect and recover from the captured
signature autonomously.

## Important limitations

- The watchdog mitigates the observed failure mode; it does not fix the underlying
  BlueZ or Bluetooth-controller defect.
- The exact root cause of the transport stall remains outside this project's
  control and may depend on BlueZ, the kernel, firmware or controller behavior.
- Automatic recovery intentionally prefers false negatives over unsafe restarts.
  Unknown or changed failure signatures therefore remain unrecovered rather than
  being guessed at.
- The systemd journal is the durable operational record. The JSON watchdog state
  keeps only current diagnostics and the rolling recovery-attempt window.

## Installation

Use the release archive attached to the GitHub release, verify its SHA-256 file,
extract it into a new directory, run the complete offline checks and reinstall
while reusing the existing configuration and protected Mesh state. Never restore
an older Bluetooth Mesh sequence-state directory during an update or rollback.
