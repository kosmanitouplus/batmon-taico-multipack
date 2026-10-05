# TAICO robustness branch — validation before merging

The branch `robustness/serial-mqtt-recovery` preserves one RS232 PACE cable,
`pace_uart:1`, `pace_uart:2`, etc. Requests still use the selected pack in header
and INFO; TAICO responses still use master ADR 01 and their INFO pack identity.
One transport, reader and transaction lock are shared. Only read commands 42
(analog) and 44 (status) are sent. Charge algorithms and MQTT command
subscriptions/discovery are disabled for PACE. No battery-write API is added.

## Recovery and MQTT

Missing serial paths are retried without ending the process. Pack failures use
independent 2, 4, 8, 16, 32, 60 second delays, capped at 60 seconds. Waiting
cycles do not increase the delay. A valid sample resets it. Each transaction
waits at most 5 seconds; a silent pack briefly occupies the bus but cannot
stop its siblings. A USB I/O error closes the obsolete descriptor; configured
paths are resolved again on reconnect. Use the existing stable
`/dev/serial/by-id/...` path where possible: replugging a path configured as
`/dev/ttyUSB0` does not follow a device that becomes `/dev/ttyUSB1`.

The former MQTT-publish watchdog and error-count exit were removed: a stopped
broker or all absent packs are valid recoverable states. The Supervisor can
still restart a genuinely stopped add-on. MQTT connects asynchronously and
retries with 2–60 second delay. Discovery is retained and replayed on reconnect.
Existing entity unique IDs, aliases and state topics are preserved. Added
availability topics are `batmon_taico/availability` (retained global LWT) and
`<existing_alias>/availability` (per pack). Home Assistant requires both online.
After a broker reconnect packs start offline until fresh samples arrive; stale
readings are never used to declare a pack online. On SIGTERM, polling tasks are
cancelled and awaited, ports/readers closed and global offline published before
MQTT disconnect. The normal shutdown exit code is zero.

Supported image architectures: aarch64 and amd64. Alpine base is pinned to
3.22, Python to the available 3.12 minor, and direct dependencies/Git references
are pinned. Existing `/app/venv/bin/python3` paths address the managed virtual
environments, not a Python minor-version site-packages directory. Optional BLE
stacks keep their existing best-effort installation behavior; a successful
PACE import does not certify those optional stacks. Transitive packages are
not fully locked. CI builds both native architectures and verifies PACE imports.

## Install the branch on the Raspberry (without changing main)

Keep the original BatMon stopped, and keep Inverter Multi-Protocol v0.1.6 running.
Do not change either inverter code or inverter serial path. The testing add-on
must use the existing BatMon slug/configuration so MQTT aliases stay unchanged.

From the HA SSH terminal, if `/addons/batmon-taico-multipack` is the existing
local checkout (check its Git remote before using it):

```sh
cd /addons/batmon-taico-multipack
git remote -v
git status --short
```

The remote must be `kosmanitouplus/batmon-taico-multipack`, and the checkout
must be clean. Preserve any local changes before switching; do not reset them.
If it is clean:

```sh
git fetch origin
git switch --track origin/robustness/serial-mqtt-recovery
ha apps reload
ha apps rebuild local_batmon_taico
ha apps info local_batmon_taico
```

If that branch already exists locally, use `git switch
robustness/serial-mqtt-recovery` followed by `git pull --ff-only` instead of
`git switch --track`. If there is no existing checkout at that path, stop here:
the local add-on may be installed at a different path. Do not create a second
copy with the same slug. This branch is not offered by the main GitHub add-on
repository until merge; the local checkout is the pre-merge test route.

Verify version `2.21-taico.2`, architecture aarch64, existing aliases/types,
and the exact battery adapter path. Build failure blocks validation.

## Required hardware acceptance test

1. **Cable absent at start:** with the battery cable still unplugged, start only
   `local_batmon_taico`. Leave it running **15 minutes** with Supervisor watchdog
   enabled. Check `ha apps info local_batmon_taico` and
   `ha apps logs local_batmon_taico`. Expect started throughout, bounded retry
   messages, no process restarts and pack entities unavailable. Inverter stays
   started; original BatMon stays stopped.
2. **Hot plug:** reconnect the existing battery RS232 cable. Without restarting
   BatMon, every connected configured pack must recover within **90 seconds**.
   Confirm distinct pack values/identity and the existing entity history.
3. **Hot unplug:** unplug the battery USB/RS232 cable. Within **30 seconds** all
   affected packs must become unavailable. Leave it absent for **5 minutes**;
   the add-on must remain started. Replug and require recovery within 90 seconds.
   Repeat twice; there must be no duplicate callbacks/pack swapping.
4. **Single pack absent:** only if an individual pack can be isolated using the
   manufacturer's safe procedure, isolate one pack's monitoring response while
   preserving the master link. Never disconnect high-current battery wiring
   for this test. Healthy packs must continue updating; only the absent one is
   offline. Restore and require recovery within 90 seconds. If this cannot be
   performed safely, report it as unvalidated hardware behavior; CI simulates it.
5. **Broker outage/restart:** in a controlled maintenance window, restart the
   MQTT broker (this affects other MQTT clients). BatMon must stay started,
   reconnect automatically, retain/replay discovery, mark packs offline until
   fresh readings, then recover their entities without new IDs.
6. **Stop:** run `ha apps stop local_batmon_taico`. Expect prompt normal stop,
   offline availability and no watchdog restart. Restart once with cable present
   and confirm all packs resume.

Collect the logs covering each transition, the add-on version, restart times
if any, and pack availability/value screenshots. Do not merge until both CI
image builds/tests pass and items 1–3, 5–6 are validated on the Raspberry.
Revert the local checkout to main and rebuild if acceptance fails; leave both
BatMon stopped while investigating. No remote Raspberry actions were taken.
