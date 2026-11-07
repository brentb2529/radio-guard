"""Tiny HA REST/WS client for smoke tests."""

import json
import time

import requests
import websocket

BASE = "http://127.0.0.1:8123"
TOKEN = open(__file__.rsplit("/", 1)[0] + "/token.txt").read().strip()
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def get(path):
    r = requests.get(BASE + path, headers=H)
    r.raise_for_status()
    return r.json()


def post(path, body=None):
    r = requests.post(BASE + path, headers=H, json=body or {})
    if not r.ok:
        print("POST", path, r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json() if r.text else None


def ws_call(msg: dict):
    ws = websocket.create_connection("ws://127.0.0.1:8123/api/websocket")
    ws.recv()
    ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
    ws.recv()
    msg = {"id": 1, **msg}
    ws.send(json.dumps(msg))
    resp = json.loads(ws.recv())
    ws.close()
    return resp


def flow(handler: str, steps: list[dict]):
    """Start a config flow and drive it with successive step inputs."""
    f = post("/api/config/config_entries/flow", {"handler": handler, "show_advanced_options": True})
    print(f"[{handler}] step={f.get('step_id')} type={f.get('type')} errors={f.get('errors')}")
    for inp in steps:
        f = post(f"/api/config/config_entries/flow/{f['flow_id']}", inp)
        print(f"[{handler}] step={f.get('step_id')} type={f.get('type')} errors={f.get('errors')} reason={f.get('reason')}")
        if f.get("type") in ("create_entry", "abort"):
            break
    return f


def service(domain, name, data=None, response=True):
    path = f"/api/services/{domain}/{name}"
    if response:
        path += "?return_response"
    return post(path, data or {})


def state(entity_id):
    return get(f"/api/states/{entity_id}")


def states(prefix=""):
    return [s for s in get("/api/states") if s["entity_id"].startswith(prefix)]


def wait_state(entity_id, pred, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = state(entity_id)
            if pred(s):
                return s
        except requests.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError(entity_id)
