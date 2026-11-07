"""Minimal fake Zigbee2MQTT bridge for smoke testing.

Publishes a retained bridge/info and answers bridge/request/backup with a
base64 zip containing an OCBF coordinator_backup.json, database.db and
configuration.yaml, exactly as Z2M does.
"""

import base64
import os
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


# ---------------------------------------------------------------- stick + data-dir mode
#
# FAKE_STICK=<name> FAKE_DATA_DIR=<dir> simulates a physical adapter plus Zigbee2MQTT's data
# folder, closely enough to exercise Radio Guard's staged (non add-on) restore end to end:
#
#   sticks/<name>.json   the adapter's NVRAM: {"ieee": ..., "network": null | {pan_id, epid, channel}}
#                        A new name is a blank stick with a random IEEE.
#   <data dir>/configuration.yaml, coordinator_backup.json, database.db   Z2M's files.
#
# On start, like zigbee-herdsman (zstack): if the stick's network matches configuration.yaml
# advanced.*, resume. Otherwise, if coordinator_backup.json matches advanced.*, restore it onto
# the stick (zstack also writes the backup's IEEE) -> "Started with result 'restored'".
# Otherwise form a new network, move database.db to database.db.backup -> 'reset'.

import random  # noqa: E402

import yaml  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIM_DEVICES = [
    {"friendly_name": "living_room_plug", "type": "Router", "ieee_address": "0xa4c138aabbccdd01", "nwk": "0x1234"},
    {"friendly_name": "hall_light", "type": "Router", "ieee_address": "0xa4c138aabbccdd02", "nwk": "0x5678"},
    {"friendly_name": "door_sensor", "type": "EndDevice", "ieee_address": "0xa4c138aabbccdd03", "nwk": "0x9abc"},
]
DEVICES = [
    {"nwk_address": d["nwk"], "ieee_address": d["ieee_address"][2:], "is_child": d["type"] == "EndDevice"}
    for d in SIM_DEVICES
]
STATE = {"result": None, "epid": None}


def _mesh_path() -> str:
    return os.path.join(HERE, "sticks", "_mesh.json")


def devices_answer() -> bool:
    """Paired devices only talk to the network they were paired on (recorded the first time it had them)."""
    mesh = json.load(open(_mesh_path())) if os.path.exists(_mesh_path()) else {}
    return bool(mesh.get("epid")) and mesh["epid"] == STATE["epid"]


def _rand_hex(n: int) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def _load_stick(name: str) -> tuple[str, dict]:
    path = os.path.join(HERE, "sticks", f"{name}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        json.dump({"ieee": "00124b00" + _rand_hex(8), "network": None}, open(path, "w"))
    return path, json.load(open(path))


def _cfg_network(cfg: dict) -> dict | None:
    adv = cfg.get("advanced") or {}
    epid = adv.get("ext_pan_id")
    if not isinstance(epid, list):
        return None
    return {"pan_id": adv.get("pan_id"), "epid": bytes(epid).hex(), "channel": adv.get("channel")}


def _ocbf_network(ocbf: dict) -> dict:
    return {"pan_id": int(ocbf["pan_id"], 16), "epid": ocbf["extended_pan_id"], "channel": ocbf["channel"]}


def boot_stick(name: str, data_dir: str) -> str:
    """Apply herdsman's start-up decision; returns 'resumed' | 'restored' | 'reset'."""
    os.makedirs(data_dir, exist_ok=True)
    stick_path, stick = _load_stick(name)
    cfg_path = os.path.join(data_dir, "configuration.yaml")
    cfg = yaml.safe_load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    cfg = cfg or {}
    cfg_net = _cfg_network(cfg)
    bak_path = os.path.join(data_dir, "coordinator_backup.json")
    bak = json.load(open(bak_path)) if os.path.exists(bak_path) else None

    if stick["network"] and cfg_net and stick["network"] == cfg_net:
        result = "resumed"
    elif bak and cfg_net and _ocbf_network(bak) == cfg_net:
        stick["network"] = cfg_net
        stick["ieee"] = bak["coordinator_ieee"]  # zstack restores write the IEEE
        result = "restored"
    else:
        stick["network"] = {"pan_id": random.randint(1, 0xFFF0), "epid": _rand_hex(16), "channel": 15}
        db = os.path.join(data_dir, "database.db")
        if os.path.exists(db):
            os.replace(db, db + ".backup")
        open(db, "w").write("{}\n")
        net = stick["network"]
        cfg.setdefault("serial", {"port": "tcp://fake:6638", "adapter": "zstack"})
        cfg["advanced"] = {
            **(cfg.get("advanced") or {}),
            "pan_id": net["pan_id"],
            "ext_pan_id": list(bytes.fromhex(net["epid"])),
            "network_key": list(bytes.fromhex("01" * 16)),
            "channel": net["channel"],
        }
        yaml.safe_dump(cfg, open(cfg_path, "w"))
        result = "reset"
    json.dump(stick, open(stick_path, "w"))
    STATE.update(result=result, epid=stick["network"]["epid"])
    if result == "resumed" and not os.path.exists(_mesh_path()):
        json.dump({"epid": stick["network"]["epid"]}, open(_mesh_path(), "w"))  # devices pair on this network

    net = stick["network"]
    OCBF.update(
        coordinator_ieee=stick["ieee"],
        pan_id=f"{net['pan_id']:04x}",
        extended_pan_id=net["epid"],
        channel=net["channel"],
        channel_mask=[net["channel"]],
        devices=DEVICES if result != "reset" else [],
    )
    json.dump(OCBF, open(bak_path, "w"))  # Z2M rewrites coordinator_backup.json after start
    INFO["coordinator"]["ieee_address"] = "0x" + stick["ieee"]
    INFO["network"] = {"pan_id": net["pan_id"], "extended_pan_id": "0x" + net["epid"], "channel": net["channel"]}
    print(f"fake z2m stick={name} ieee={stick['ieee']} epid={net['epid']} Started with result '{result}'", flush=True)
    return result


def make_zip_from_dir(data_dir: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(data_dir)):
            full = os.path.join(data_dir, name)
            if os.path.isfile(full):
                zf.write(full, name)
    return buf.getvalue()


def _stick_from_config(data_dir: str) -> str | None:
    """serial.port tcp://<stick>:6638 in configuration.yaml names the plugged-in stick (like a real port)."""
    cfg_path = os.path.join(data_dir, "configuration.yaml")
    if not os.path.exists(cfg_path):
        return None
    port = str(((yaml.safe_load(open(cfg_path)) or {}).get("serial") or {}).get("port") or "")
    if port.startswith("tcp://"):
        return port[len("tcp://") :].split(":")[0] or None
    return None


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    data_dir = os.environ.get("FAKE_DATA_DIR")
    stick = os.environ.get("FAKE_STICK") or (data_dir and _stick_from_config(data_dir))
    if stick and data_dir:
        boot_stick(stick, data_dir)
    if os.environ.get("FAKE_IEEE"):
        INFO["coordinator"]["ieee_address"] = os.environ["FAKE_IEEE"]
        OCBF["coordinator_ieee"] = os.environ["FAKE_IEEE"].replace("0x", "")
    if os.environ.get("FAKE_EPID"):
        INFO["network"]["extended_pan_id"] = os.environ["FAKE_EPID"]
        OCBF["extended_pan_id"] = os.environ["FAKE_EPID"].replace("0x", "")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fake-z2m")

    def on_connect(c, u, f, rc, props=None):
        print("fake z2m connected", rc, flush=True)
        c.publish(f"{BASE}/bridge/state", json.dumps({"state": "online"}), retain=True)
        c.publish(f"{BASE}/bridge/info", json.dumps(INFO), retain=True)
        known = [] if STATE["result"] in (None, "reset") else SIM_DEVICES
        devices = [{"friendly_name": "Coordinator", "type": "Coordinator", "ieee_address": INFO["coordinator"]["ieee_address"]}]
        devices += [{k: v for k, v in d.items() if k != "nwk"} for d in known]
        c.publish(f"{BASE}/bridge/devices", json.dumps(devices), retain=True)
        c.subscribe(f"{BASE}/bridge/request/backup")
        c.subscribe(f"{BASE}/+/get")
        if known and devices_answer():
            import threading  # noqa: PLC0415

            def _wake():  # the battery sensor wakes up a little later and reports
                time.sleep(8)
                c.publish(f"{BASE}/door_sensor", json.dumps({"contact": True, "battery": 87, "linkquality": 90}))

            threading.Thread(target=_wake, daemon=True).start()

    def on_message(c, u, msg):
        print("fake z2m got", msg.topic, msg.payload[:80], flush=True)
        if msg.topic.endswith("/get"):
            name = msg.topic[len(BASE) + 1 : -len("/get")]
            dev = next((d for d in SIM_DEVICES if d["friendly_name"] == name), None)
            if dev and dev["type"] == "Router" and STATE["result"] not in (None, "reset") and devices_answer():
                c.publish(f"{BASE}/{name}", json.dumps({"state": "ON", "linkquality": 120}))
            return
        try:
            req = json.loads(msg.payload or b"{}")
        except ValueError:
            req = {}
        if mode == "error":
            resp = {"data": {}, "status": "error", "error": "Simulated backup failure"}
        elif mode == "silent":
            return
        else:
            zipped = make_zip_from_dir(data_dir) if data_dir else make_zip()
            resp = {"data": {"zip": base64.b64encode(zipped).decode()}, "status": "ok"}
        if isinstance(req, dict) and "transaction" in req:
            resp["transaction"] = req["transaction"]
        c.publish(f"{BASE}/bridge/response/backup", json.dumps(resp))

    client.on_connect = on_connect
    client.on_message = on_message
    # Like real Z2M: retained "offline" via last will, and on a clean stop.
    client.will_set(f"{BASE}/bridge/state", json.dumps({"state": "offline"}), retain=True)
    client.connect(os.environ.get("FAKE_BROKER", "127.0.0.1"), int(os.environ.get("FAKE_BROKER_PORT", "1883")), 60)
    client.loop_start()

    import signal  # noqa: PLC0415

    def _stop(*_):
        info = client.publish(f"{BASE}/bridge/state", json.dumps({"state": "offline"}), retain=True)
        info.wait_for_publish(2)
        client.disconnect()
        print("fake z2m stopped", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
