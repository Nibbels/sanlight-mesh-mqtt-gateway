# Conservative automatic Mesh recovery

BlueZ 5.82 can remain alive, receive BLE advertisements and accept Mesh D-Bus
requests while its outgoing HCI advertising path has stopped. The captured
failure is tracked upstream as [bluez/bluez#2353](https://github.com/bluez/bluez/issues/2353).
Restarting `bluetooth-meshd` restores communication, but restarting on every lamp
timeout would be unsafe because powered-off lamps and RF loss look similar at the
application layer.

The installed `sanlight-mesh-watchdog.timer` therefore uses a fail-closed,
read-only confirmation sequence:

1. The gateway must already have recorded a recent successful lamp response.
2. A complete read-only command targeting all configured lamps must end as
   `mesh-no-response`.
3. The watchdog stops only the MQTT gateway worker and performs one independent
   `get-live` probe against a configured lamp. It does not restart BlueZ first.
4. The PL011 UART TX counter is sampled immediately before and after that probe.
5. Recovery is allowed only when BlueZ accepted the probe, no status arrived and
   the UART transmitted exactly zero bytes. This is the measured TX-stall
   signature. If bytes were transmitted, the watchdog treats the incident as an
   ordinary no-reply condition and does not restart the Mesh daemon.
6. The existing `sanlight-gateway recover-mesh` sequence restarts the local Mesh
   transport and gateway. A new read-only lamp response must then verify the
   recovery.

No original command is replayed. In particular, write commands are never
retried by the watchdog. Missing UART counters, ambiguous probe output, a
single-node failure, an old incident or an inactive service all fail closed
without recovery.

## Loop protection

Defaults permit at most two automatic recoveries in a six-hour rolling window,
with a 30-minute recovery cooldown. Active confirmation probes are limited to
one per hour. The private state is stored as
`mesh-watchdog-state.json` beside the other gateway state files.

The timer runs once per minute, but it remains silent and sends no Mesh traffic
while no eligible incident exists.

## Configuration

Existing configurations enable the watchdog with conservative defaults. Add an
optional table to override them:

```toml
[watchdog]
enabled = true
incident_max_age_seconds = 3600
recent_success_seconds = 86400
probe_timeout_seconds = 50
probe_cooldown_seconds = 3600
recovery_cooldown_seconds = 1800
recovery_window_seconds = 21600
max_recoveries_in_window = 2
verification_timeout_seconds = 60
```

Set `enabled = false` and reinstall the service to keep the timer installed but
make every invocation a no-op. To stop scheduling entirely:

```bash
sudo systemctl disable --now sanlight-mesh-watchdog.timer
```

## Inspection

```bash
systemctl status sanlight-mesh-watchdog.timer
sudo journalctl -u sanlight-mesh-watchdog.service --since today
sudo cat /path/to/state/mesh-watchdog-state.json
```

The watchdog state contains timestamps, outcomes and UART counters, but no Mesh
keys, MQTT credentials or raw Bluetooth traffic.
