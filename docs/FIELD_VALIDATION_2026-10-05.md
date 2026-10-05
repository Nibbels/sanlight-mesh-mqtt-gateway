# External field validation — 2026-10-05

This document archives the first detailed external end-to-end validation report
for `sanlight-mesh-mqtt-gateway` 0.4.2. The report was provided by GitHub user
`@Schlimmer` in the public feedback thread:

https://github.com/Nibbels/ioBroker.sanlightmesh/issues/1#issuecomment-5992258019

The evidence below is intentionally limited to non-secret observations. No
NetKey, AppKey, DeviceKey, BlueZ token, private CDB content or MQTT credential is
recorded here.

## Test environment

- gateway release: `0.4.2`
- gateway commit: `1d7555d296cf312a3c982cb7804f7f02acddc364`
- Raspberry Pi Zero 2 W
- Debian 13 (`trixie`), aarch64
- BlueZ 5.82
- fresh local BlueZ Mesh state before installation
- existing SANlight Mesh with two dimmers and two groups
- SANlight dimmer firmware: previous firmware generation; exact numeric version
  was not obtained from a trusted read-only source
- the newly released SANlight dimmer firmware was deliberately not installed, so
  this report does not validate that newer firmware

## Independent IV Index verification

The SANlight app export did not contain a top-level `ivIndex`, and the new
gateway had no existing BlueZ Mesh database from which to recover one. The public
installer correctly stopped rather than guessing an IV Index.

A passive `btmon` capture made while the SANlight app generated normal Mesh
traffic contained repeated Secure Network Beacons. One observed beacon was:

```text
01 00 0c4e51dbc4f59efa 00000000 92ae81774d5f1bfa
```

The external tester then performed a local, read-only cryptographic check against
the private exported `SANlightMesh.json` without printing Mesh keys:

```text
Primary NetKey found: yes
Derived Network ID: 0c4e51dbc4f59efa
Network ID match: YES
Beacon authentication: VALID
Flags: 0x00
IV Index: 0
```

This ties the captured beacon to the exact CDB/network and independently verifies
that this network was using IV Index `0` at the time of the capture.

Project policy remains:

- IV Index is network-wide and is not tied to SANlight App-ID 1/2.
- `0` is not a universal default for existing meshes and must not be guessed.
- sender/source replay state is the separate 24-bit Bluetooth Mesh Sequence Number.
- when no trusted CDB or BlueZ IV source exists, an explicit `--iv-index` should
  be used only after the current network value has been independently verified.

The verified installation therefore used:

```bash
sudo bash scripts/install-gateway.sh --iv-index 0
```

and completed successfully without resetting or reprovisioning the existing Mesh.

## Identity and replay observations

The installation intentionally used separate SANlight identities:

- phone/control identity: App-ID 1 / source `0x2400`
- gateway sender identity: App-ID 2 / source `0x2800`

No replay-protection or Sequence Number failure was observed after the fresh
BlueZ import. This is useful evidence for the separate-sender design, but it is
not proof that replay-state problems can never occur on other installations or
previously used source identities.

## Read and write validation

The external installation successfully read real lamp state for:

- live output;
- MaxBrightness; and
- stored Daylight configuration.

A reversible MaxBrightness test `40 -> 39 -> 40 %` succeeded and was independently
confirmed by readback.

On one restore write the explicit SANlight `0x07` acknowledgement was not
observed, while the subsequent GetMax readback returned the requested value.
This independently supports the current gateway policy that verified readback is
the stronger success criterion and that a missing acknowledgement alone does not
prove that the write failed.

The tester also confirmed that the stored SANlight Daylight profile remained
intact while MaxBrightness acted as its limiter/scaler.

## MQTT and Home Assistant validation

The MQTT API was used successfully outside the ioBroker adapter:

- a gateway `refresh` command produced verified node state/result data;
- Home Assistant was bridged to the gateway over MQTT;
- Home Assistant successfully changed MaxBrightness and received verified
  readback state; and
- Home Assistant successfully invoked `set-clock` and received a verified result
  within the gateway tolerance.

This is external evidence that MQTT API v1 is usable by a non-ioBroker client.
Home Assistant is not currently a separately supported/configured product
integration, so this should be treated as protocol-interoperability evidence,
not as a promise of a maintained Home Assistant adapter.

## Lamp clock observation

In this installation, `lampClock` behaved as the lamp's elapsed/day-cycle timer
since power-up and was the timebase used by the stored SANlight Daylight Cycle.
The tester therefore deliberately did not synchronize it continuously to civil
wall-clock time.

This agrees with the project's earlier observations, but remains empirical
SANlight behavior rather than a published vendor protocol guarantee.

## What this report proves

This external report materially extends the project's hardware evidence:

- Pi Zero 2 W / Debian 13 / BlueZ 5.82 can run the 0.4.2 gateway productively;
- a missing CDB `ivIndex` can be resolved non-destructively by a cryptographically
  authenticated Secure Network Beacon belonging to the same NetKey/CDB;
- a fresh BlueZ import can coexist with the existing SANlight Mesh using the
  separately selected App-ID 2 sender identity;
- core reads, reversible MaxBrightness writes and readback verification work on
  a second independently operated installation;
- the stored Daylight profile plus MaxBrightness scaling model was independently
  reproduced; and
- MQTT API v1 interoperated successfully with Home Assistant.

## Limits and follow-up ideas

This remains one external installation, not a compatibility matrix. In
particular:

- the exact old dimmer firmware version is unknown;
- the newly released SANlight dimmer firmware remains unvalidated;
- the report does not prove all Mesh layouts, RF conditions or historical replay
  states are safe; and
- Home Assistant interoperability was user-built and is not yet an officially
  maintained integration.

A useful future improvement is a local read-only Secure Network Beacon verifier
that accepts a captured beacon, derives the expected Network ID and BeaconKey
from the private CDB, validates the authentication value, and prints only
non-secret results. That would make the currently manual IV Index verification
workflow reproducible without weakening the installer's fail-closed policy.
