import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import paho.mqtt.client as mqtt
import pytz
import yaml
from prometheus_client import Gauge, start_http_server

STATE_PATH = "/data/state.json"
DEFAULT_CONFIG_PATHS = ("config.yaml", "/data/config.yaml")
METRICS_PORT = 9500


@dataclass(frozen=True)
class MQTTConfig:
    host: str
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    topic: str
    ip: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def state_key(self) -> str:
        return self.topic


@dataclass(frozen=True)
class AppConfig:
    debug: bool
    mqtt: MQTTConfig
    devices: list[DeviceConfig]


def default_device_state() -> dict[str, Any]:
    return {"carry": 0.0, "last": 0.0, "apply_correction": False}


def resolve_config_path() -> Optional[str]:
    for path in DEFAULT_CONFIG_PATHS:
        if os.path.exists(path):
            return path
    return None


def load_config() -> AppConfig:
    config_path = resolve_config_path()
    if not config_path:
        raise FileNotFoundError(
            "No config.yaml found. Copy config.example.yaml to config.yaml and fill in your devices."
        )

    with open(config_path, "r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    mqtt_config = raw_config.get("mqtt", {})
    device_entries = raw_config.get("devices", [])
    if not device_entries:
        raise ValueError(f"No devices configured in {config_path}")

    devices: list[DeviceConfig] = []
    seen_topics: set[str] = set()
    for index, entry in enumerate(device_entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Device #{index} in {config_path} must be a mapping")

        name = str(entry.get("name", "")).strip()
        topic = str(entry.get("topic", "")).strip()
        if not name or not topic:
            raise ValueError(f"Device #{index} in {config_path} needs name and topic")

        auth = entry.get("auth", {}) or {}
        if not isinstance(auth, dict):
            raise ValueError(f"Device #{index} in {config_path} must use auth as a mapping")
        if topic in seen_topics:
            raise ValueError(f"Duplicate topic '{topic}' in {config_path}")
        seen_topics.add(topic)
        devices.append(
            DeviceConfig(
                name=name,
                topic=topic,
                ip=entry.get("ip") or entry.get("host"),
                username=auth.get("username"),
                password=auth.get("password"),
            )
        )

    return AppConfig(
        debug=bool(raw_config.get("debug", False)),
        mqtt=MQTTConfig(
            host=str(mqtt_config.get("host", "localhost")),
            port=int(mqtt_config.get("port", 1883)),
            username=mqtt_config.get("username"),
            password=mqtt_config.get("password"),
        ),
        devices=devices,
    )


def coerce_device_state(raw_state: Any) -> dict[str, Any]:
    state = default_device_state()
    if not isinstance(raw_state, dict):
        return state

    state["carry"] = float(raw_state.get("carry", state["carry"]))
    state["last"] = float(raw_state.get("last", state["last"]))
    state["apply_correction"] = bool(raw_state.get("apply_correction", state["apply_correction"]))
    return state


def load_state(device_topics: list[str]) -> dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            raw_state = json.load(handle)
            print(f"📥 Loaded state from disk: {raw_state}")
    except (FileNotFoundError, json.JSONDecodeError):
        raw_state = {}
        print("🆕 No previous state found — starting fresh")

    state = {"version": 2, "devices": {}}

    if isinstance(raw_state, dict) and isinstance(raw_state.get("devices"), dict):
        for topic in device_topics:
            state["devices"][topic] = coerce_device_state(raw_state["devices"].get(topic, {}))
        return state

    if isinstance(raw_state, dict) and {"carry", "last", "apply_correction"} <= raw_state.keys():
        if device_topics:
            state["devices"][device_topics[0]] = coerce_device_state(raw_state)
        for topic in device_topics[1:]:
            state["devices"][topic] = default_device_state()
        return state

    for topic in device_topics:
        state["devices"][topic] = default_device_state()
    return state


def save_state(state: dict[str, Any]) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        print(f"💾 State saved: {state}")
    except Exception as exc:
        print("💥 Error saving state:", exc)


def extract_topic(msg_topic: str) -> Optional[str]:
    parts = msg_topic.split("/")
    if len(parts) == 3 and parts[0] == "tele" and parts[2] == "SENSOR":
        return parts[1]
    return None


def build_metric() -> Gauge:
    return Gauge(
        "tasmota_energy_today_corrected",
        "Corrected ENERGY.Today",
        ["device", "topic", "ip"],
    )


def log_info(message: str) -> None:
    print(message)


def log_debug(message: str) -> None:
    if config.debug:
        print(f"🐞 {message}")


def create_mqtt_client() -> mqtt.Client:
    callback_api_version = getattr(mqtt, "CallbackAPIVersion", None)
    if callback_api_version is not None:
        return mqtt.Client(callback_api_version=callback_api_version.VERSION2)
    return mqtt.Client()


config = load_config()
state = load_state([device.topic for device in config.devices])
local_tz = pytz.timezone("Asia/Kolkata")
corrected = build_metric()
mqtt_connected = Gauge("tasmota_mqtt_connected", "MQTT connection status")
mqtt_last_message = Gauge("tasmota_mqtt_last_message_unixtime", "Last MQTT message timestamp")
device_by_topic = {device.topic: device for device in config.devices}
device_metrics = {}

start_http_server(METRICS_PORT)
log_info(f"🚀 /metrics exposed on :{METRICS_PORT}")
log_info(f"🧩 Loaded config for {len(config.devices)} device(s) from config.yaml")
log_debug(f"MQTT broker: {config.mqtt.host}:{config.mqtt.port}")
for device in config.devices:
    log_debug(
        f"Device loaded: name={device.name}, topic={device.topic}, ip={device.ip or '-'}"
    )

for device in config.devices:
    device_metrics[device.topic] = corrected.labels(
        device=device.name,
        topic=device.topic,
        ip=device.ip or "",
    )
    device_metrics[device.topic].set(0.0)

last_update = datetime.now(local_tz)


def on_connect(
    client: mqtt.Client,
    userdata: Any,
    flags: Any,
    reason_code: Any,
    properties: Any = None,
) -> None:
    if reason_code != 0:
        mqtt_connected.set(0)
        log_info(f"⚠️ MQTT connect failed with reason code: {reason_code}")
        return

    mqtt_connected.set(1)
    log_info("✅ MQTT connected")
    for device in config.devices:
        topic = f"tele/{device.topic}/SENSOR"
        client.subscribe(topic)
        log_info(f"📡 Subscribed to {topic}")
        log_debug(f"Subscription target device={device.name}")


def on_disconnect(
    client: mqtt.Client,
    userdata: Any,
    reason_code: Any,
    properties: Any = None,
) -> None:
    mqtt_connected.set(0)
    log_info(f"⚠️ MQTT disconnected: {reason_code}")


def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    global state, last_update

    now = datetime.now(local_tz)
    last_update = now
    mqtt_last_message.set(now.timestamp())
    hour_now = now.hour

    device_topic = extract_topic(msg.topic)
    if not device_topic or device_topic not in device_by_topic:
        log_info(f"⚠️ Ignoring message for unknown topic: {msg.topic}")
        return

    device = device_by_topic[device_topic]
    device_state = state["devices"].setdefault(device_topic, default_device_state())

    try:
        log_debug(f"Incoming message topic={msg.topic}, bytes={len(msg.payload)}")
        payload = json.loads(msg.payload.decode())
        log_debug(f"Decoded payload for {device.name}: {payload}")
        if "ENERGY" not in payload:
            log_info(f"⚠️ No ENERGY data in payload: {payload}")
            return

        today = float(payload["ENERGY"].get("Today", 0.0))
        corrected_today = device_state["carry"] + today

        # Preserve energy totals when the device reports a reset after a powercut.
        if abs(today) <= 1e-2 and hour_now != 0:
            if device_state["last"] - today > 0 and not device_state["apply_correction"]:
                device_state["carry"] += device_state["last"]
                device_state["apply_correction"] = True
                corrected_today = device_state["carry"] + today
                log_info(
                    f"⚡ Reset detected for {device.name}. "
                    f"Carry={device_state['carry']}, Raw={repr(today)}"
                )
            elif device_state["apply_correction"]:
                corrected_today = device_state["carry"] + today
                device_state["apply_correction"] = False
            else:
                log_debug(
                    f"📦 {device.name}: today={today}, corrected={corrected_today}, "
                    f"carry={device_state['carry']}, last={device_state['last']}"
                )
        else:
            log_info(f"✅ {device.name}: normal tracking at {hour_now:02d}:00")
            log_info(f"📈 Raw today: {today}")
            log_info(f"✅ Corrected: {corrected_today}")

        device_metrics[device_topic].set(corrected_today)
        device_state["last"] = today

        if hour_now == 0:
            log_info(f"🕛 Midnight reset for {device.name}")
            device_state["carry"] = 0.0
            device_state["apply_correction"] = False

        save_state(state)
    except Exception as exc:
        log_info(f"💥 Error in message handler: {exc}")

client = create_mqtt_client()
if config.mqtt.username and config.mqtt.password:
    client.username_pw_set(config.mqtt.username, config.mqtt.password)

client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message
client.reconnect_delay_set(min_delay=2, max_delay=60)
client.connect_async(config.mqtt.host, config.mqtt.port, 60)
client.loop_start()
log_info(f"🚀 Connecting to MQTT broker at {config.mqtt.host}:{config.mqtt.port}")
log_debug(f"Config debug mode is enabled; will log decoded MQTT payloads and state transitions")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("👋 Shutting down")
finally:
    client.loop_stop()
    client.disconnect()
