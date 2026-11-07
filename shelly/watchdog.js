// Radio Guard power watchdog for Shelly Gen2/Gen3 (Plus Plug, Plus 1, Pro 1, Plug S Gen3).
//
// Put the Home Assistant box's power supply on this Shelly. The script listens for
// Radio Guard's MQTT heartbeat; if it stops for `timeoutS` while the broker is still
// reachable, it cuts power for `offS` seconds, then leaves HA alone for `cooldownS`.
//
// Setup:
//   1. Shelly web UI -> Settings -> MQTT: enable, point at the SAME broker HA uses.
//   2. Settings -> Input/Output -> set "default power-on state" to ON.
//   3. Scripts -> Add script -> paste this -> Save -> enable "Run on startup" -> Start.
//
// Safety notes:
//   - Broker unreachable != HA dead. The script does nothing while MQTT is disconnected.
//   - It never fires until it has seen at least one heartbeat since the Shelly booted,
//     so a Shelly reboot while HA is already hung will NOT trigger a cycle. That is
//     deliberate; accept the trade-off or lower `bootGraceS` to opt in.
//   - `toggle_after` is used for the OFF period, so power comes back even if the
//     script crashes mid-cycle.
//   - Max `maxCyclesPerDay` cycles, then it stops. A box that cannot boot should not be
//     power-cycled forever (SD cards die that way).
//   - If the broker runs ON the HA box, a hung HA takes the broker with it and this
//     watchdog (correctly) stays idle. Put the broker elsewhere, or enable `httpFallback`.

let CFG = {
  topic: "radio_guard/heartbeat",
  timeoutS: 300,        // seconds without heartbeat before cycling
  cooldownS: 3600,      // seconds after a cycle before another is allowed
  offS: 15,             // seconds to hold power off
  maxCyclesPerDay: 3,
  bootGraceS: 0,        // >0: after this many seconds with MQTT connected and no heartbeat, treat as silent
  httpFallback: "",     // e.g. "http://192.168.1.10:8123/api/" - 2xx/401 counts as alive
  switchId: 0,
};

let bootAt = Date.now();
let lastBeat = 0;
let lastCycle = 0;
let seen = 0;
let cyclesToday = 0;
let dayStart = Date.now();

MQTT.subscribe(CFG.topic, function () {
  lastBeat = Date.now();
  seen++;
});

function cycle(reason) {
  print("radio-guard: " + reason + "; cycling HA power");
  lastCycle = Date.now();
  cyclesToday++;
  Shelly.call("Switch.Set", { id: CFG.switchId, on: false, toggle_after: CFG.offS });
  lastBeat = Date.now(); // give HA a full timeout to boot
}

function tick() {
  let now = Date.now();
  if (now - dayStart > 86400000) { cyclesToday = 0; dayStart = now; }
  if (cyclesToday >= CFG.maxCyclesPerDay) return;
  if (lastCycle !== 0 && (now - lastCycle) / 1000 < CFG.cooldownS) return;

  if (!MQTT.isConnected()) return; // broker down != HA down

  if (seen === 0) {
    if (CFG.bootGraceS <= 0) return;
    if ((now - bootAt) / 1000 < CFG.bootGraceS) return;
    // fall through: connected to broker, never heard HA, grace expired
  } else if ((now - lastBeat) / 1000 < CFG.timeoutS) {
    return;
  }

  if (CFG.httpFallback !== "") {
    Shelly.call("HTTP.GET", { url: CFG.httpFallback, timeout: 5 }, function (res, err) {
      if (err === 0 && res && (res.code === 200 || res.code === 401)) {
        lastBeat = Date.now(); // HA answers HTTP; broker path is what is broken
        return;
      }
      cycle("no heartbeat and HTTP check failed");
    });
    return;
  }
  cycle("no heartbeat for " + Math.floor((now - lastBeat) / 1000) + " s");
}

Timer.set(10000, true, tick);
