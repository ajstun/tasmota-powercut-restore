# tasmota-powercut-restore

A small MQTT-to-Prometheus exporter that keeps `ENERGY.Today` stable across Tasmota power cuts by persisting the last known value on disk.

## What it does

- Subscribes to `tele/<topic>/SENSOR` for one or more Tasmota devices.
- Persists corrected daily energy totals in `/data/state.json`.
- Exposes `tasmota_energy_today_corrected` on `/metrics` for Prometheus.

## Setup

1. Copy `config.example.yaml` to `config.yaml`.
2. Fill in your MQTT broker details, device list, and any per-device metadata. Use the broker's LAN IP/hostname, not `localhost`, inside Docker/Unraid.
3. Keep `config.yaml` out of git.
4. Start the container or run `python main.py`.

When using Docker Compose, `config.yaml` is mounted from the repo root and state is stored in `./data`.
You can also pull the published image directly:

```bash
docker pull ghcr.io/ajstun/tasmota-powercut-restore:latest
docker compose pull
docker compose up -d
```

If the MQTT broker is down when the container starts, the exporter stays up and keeps retrying until the broker is reachable.
If you use Unraid, keep the stack image-only; `build:` makes it try a local build and look for a Dockerfile in the appdata path.
Set `debug: true` in `config.yaml` to print MQTT connect/disconnect events, subscriptions, payload sizes, decoded payloads, and device state changes.

## Adding a device once

Add one entry under `devices:` and restart the service:

```yaml
devices:
  - name: kitchen-plug
    topic: tasmota_kitchen
    ip: <device-ip>
    auth:
      username: <device-username>
      password: <device-password>
```

Required fields:

- `name`: label shown in Prometheus
- `topic`: the Tasmota MQTT topic suffix used in `tele/<topic>/SENSOR`

Optional metadata:

- `ip`: device IP address
- `auth.username` / `auth.password`: device credentials you want to keep alongside the entry

## Files

- `config.yaml`: local runtime config
- `/data/state.json`: persisted correction state

## Prometheus metric

- `tasmota_energy_today_corrected{device,topic,ip}`
- `tasmota_mqtt_connected`
- `tasmota_mqtt_last_message_unixtime`
