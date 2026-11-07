"""Onboard the smoke-test HA instance and save a token to token.txt."""

import json
import sys

import requests

BASE = "http://127.0.0.1:8123"
CLIENT_ID = "http://127.0.0.1:8123/"


def main() -> None:
    s = requests.Session()
    r = s.post(
        f"{BASE}/api/onboarding/users",
        json={"client_id": CLIENT_ID, "name": "Smoke", "username": "smoke",
              "password": "smoke-password-123", "language": "en"},
    )
    if r.status_code == 403:
        print("already onboarded; use existing token.txt")
        return
    r.raise_for_status()
    code = r.json()["auth_code"]
    r = s.post(f"{BASE}/auth/token", data={"client_id": CLIENT_ID, "grant_type": "authorization_code", "code": code})
    r.raise_for_status()
    access = r.json()["access_token"]
    h = {"Authorization": f"Bearer {access}"}
    for step, body in [
        ("core_config", {}),
        ("analytics", {}),
        ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
    ]:
        r = s.post(f"{BASE}/api/onboarding/{step}", json=body, headers=h)
        print(step, r.status_code)
        if step == "integration" and r.ok:
            code = r.json()["auth_code"]
            r = s.post(f"{BASE}/auth/token",
                       data={"client_id": CLIENT_ID, "grant_type": "authorization_code", "code": code})
            access = r.json()["access_token"]
            h = {"Authorization": f"Bearer {access}"}
    # Long-lived token so later scripts don't expire
    import websocket  # noqa

    ws = websocket.create_connection(f"ws://127.0.0.1:8123/api/websocket")
    ws.recv()
    ws.send(json.dumps({"type": "auth", "access_token": access}))
    ws.recv()
    ws.send(json.dumps({"id": 1, "type": "auth/long_lived_access_token", "client_name": "smoke", "lifespan": 365}))
    tok = json.loads(ws.recv())["result"]
    ws.close()
    open("token.txt", "w").write(tok)
    print("token saved")


if __name__ == "__main__":
    sys.exit(main())
