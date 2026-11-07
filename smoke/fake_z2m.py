"""Minimal fake Zigbee2MQTT bridge for smoke testing.

Publishes a retained bridge/info and answers bridge/request/backup with a
base64 zip containing an OCBF coordinator_backup.json, database.db and
configuration.yaml, exactly as Z2M does.
"""

import base64
import io
import json
import sys
import time
import zipfile

import paho.mqtt.client as mqtt

BASE = "zigbee2mqtt"

OCBF = {
    "metadata": {"format": "zigpy/open-coordinator-backup", "version": 1, "source": "zigbee2mqtt@2.8.0",
                 "internal": {"zhVersion": "7.0.0"}},
    "stack_specific": {"zstack": {"tclk_seed": "aa" * 16}},
    "coordinator_ieee": "00124b0029aa55ff",
    "pan_id": "1a62",
    "extended_pan_id": "dddddddddddddddd",
    "nwk_update_id": 0,
    "security_level": 5,
    "channel": 15,
    "channel_mask": [15],
    "network_key": {"key": "01" * 16, "sequence_number": 0, "frame_counter": 123456},
    "devices": [
        {"nwk_address": "0x1234", "ieee_address": "a4c138aabbccdd01", "is_child": True},
        {"nwk_address": "0x5678", "ieee_address": "a4c138aabbccdd02", "is_child": False},
    ],
}

INFO = {
    "version": "2.8.0",
    "zigbee_herdsman_version": "7.0.0",
    "zigbee_herdsman_converters_version": "25.0.0",
    "commit": "abcdef",
    "coordinator": {"ieee_address": "0x00124b0029aa55ff", "type": "zStack3x0",
                    "meta": {"revision": 20240710, "majorrel": 2, "minorrel": 7}},
    "network": {"pan_id": 6754, "extended_pan_id": "0xdddddddddddddddd", "channel": 15},
    "permit_join": False,
    "restart_required": False,
}


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("coordinator_backup.json", json.dumps(OCBF))
        zf.writestr("database.db", "{}\n")
        zf.writestr("configuration.yaml", "homeassistant: true\nmqtt:\n  server: mqtt://localhost\n")
    return buf.getvalue()


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fake-z2m")

    def on_connect(c, u, f, rc, props=None):
        print("fake z2m connected", rc, flush=True)
        c.publish(f"{BASE}/bridge/state", json.dumps({"state": "online"}), retain=True)
        c.publish(f"{BASE}/bridge/info", json.dumps(INFO), retain=True)
        c.subscribe(f"{BASE}/bridge/request/backup")

    def on_message(c, u, msg):
        print("fake z2m got", msg.topic, msg.payload[:80], flush=True)
        try:
            req = json.loads(msg.payload or b"{}")
        except ValueError:
            req = {}
        if mode == "error":
            resp = {"data": {}, "status": "error", "error": "Simulated backup failure"}
        elif mode == "silent":
            return
        else:
            resp = {"data": {"zip": base64.b64encode(make_zip()).decode()}, "status": "ok"}
        if isinstance(req, dict) and "transaction" in req:
            resp["transaction"] = req["transaction"]
        c.publish(f"{BASE}/bridge/response/backup", json.dumps(resp))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect("127.0.0.1", 1883, 60)
    client.loop_start()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
