# C4 diagnostics uploader

This standalone client uploads one to eight C4 diagnostic files to the dedicated DAYOU endpoint. It does not use the DVR, LLM, mail, or operator-review pipelines.

## Private configuration

Create `/data/c4-diagnostics.json` on the C4 and restrict it to the device owner with mode `0600`.

```json
{
  "api_key": "replace-with-the-private-C4-key",
  "source_id": "replace-with-the-C4-source-id"
}
```

The key may instead be supplied through `C4_DIAGNOSTICS_API_KEY`. Do not pass it on the command line or commit the configuration file. The following settings are optional.

- `C4_DIAGNOSTICS_API_URL` defaults to the production C4 endpoint.
- `C4_DIAGNOSTICS_AUTH_HEADER` defaults to `X-C4-API-Key`.
- `C4_DIAGNOSTICS_AUTH_SCHEME` supports values such as `Bearer` when required.
- `C4_DIAGNOSTICS_FILE_FIELD` defaults to `files`.
- `C4_DIAGNOSTICS_SOURCE_ID` supplies the device source ID.

## Upload

```sh
python3 tools/c4_diagnostics/upload.py \
  --software-version carrot-wip-custom \
  --note "manual C4 diagnostic capture" \
  /path/to/tmux.log /path/to/memory.log
```

The client generates a UUID when `--upload-id` is omitted. Reuse an explicitly supplied upload ID when retrying the same files. It rejects invalid UUIDs, more than eight files, and payloads larger than 5 MiB before contacting the server.

## Automatic K7 radar capture

On a device, a valid private configuration enables capture whenever this branch's on-road service runs. The service captures classical CAN addresses `0x500` through `0x53f`, the SCC addresses `0x389`, `0x420`, `0x421`, and `0x50a`, and radar diagnostic addresses `0x7d0`/`0x7d8`. It also passively subscribes to `sendcan` for outgoing requests to `0x7d0`. It does not send CAN messages or change driving control.

Up to 256 diagnostic frames observed before on-road capture starts are buffered in memory and included with the next capture. Earlier frames can be discarded when this buffer is full; messages sent before the service subscribes cannot be recovered. Raw ISO-TP bytes, bus and timestamps retain the existing `C4RADAR1` format. Use `decode.py` to inspect them; the scene track count is not a diagnostic success indicator. This uploader remains specific to `carrot-wip-custom`.

Completed captures are rotated at 60 seconds or 2 MiB, whichever comes first. Each upload includes the original radar CAN capture, a bounded `.c4scene` JSON Lines companion with synchronized `liveTracks`, model path, lane, radar lead, speed, and longitudinal-control state, and a small `/proc/meminfo` snapshot. The scene companion does not include camera video and does not alter vehicle control. Regular radar and scene collection occurs only while the car is on-road; the bounded diagnostic buffer described above may include preceding off-road frames. Completed captures upload whenever `deviceState` reports a network connection, including after the car goes off-road. Failed uploads retain the same deterministic UUID and are retried without deleting the local originals. Incomplete radar-only fragments are preserved locally and are not uploaded.

There is no time limit. Capturing and uploading stop when this branch's service is no longer running, such as after switching the device back to a branch without the C4 diagnostics process.

## Automatic read-only K7 inventory

With the private C4 configuration installed, `card` performs at most one inventory attempt per
device boot, before `FirmwareQueryDone`, CarParams publication and normal control initialization.
It requires one connected Panda already in the existing ELM327 fingerprinting safety mode,
ignition on, fresh valid vehicle/Panda data, P gear, zero wheel/vehicle speeds, accelerator released,
cruise inactive and controls not ready. The conditions must hold for one second within a three-second
startup window. Movement, an unsafe/unknown state or an already enabled radar-tracks setting prevents
the query. Each transmit, including ISO-TP flow control, rechecks the conditions.

The inventory reads `22 F1 00`, then `22 01 42` after a positive F100 response, followed by optional
part/software identifiers `22 F1 87` and `22 F1 95`, on address `0x7d0`, bus 0.
Each read has a 0.5-second total query budget; there is no retry, diagnostic-session change, write,
ECU disable command, radar activation, Panda safety-mode change or safety-policy modification.
The normal startup continues after the attempt. If conditions were missed, restart of `card` in the
same device boot does not retry. A later device boot permits a new attempt.

`radar-inventory-<boot-id>.json` records positive responses, NRCs, no-positive-response, aborted,
skipped or interrupted outcomes and bounded raw diagnostic frames. Results upload independently of
driving captures, even when no scene exists. Failed uploads use the existing stable UUID and retry
policy. Original files stay on the device. The server's generic rules may not interpret the inventory;
download the original JSON to inspect it. A successful read is not proof that tracks are supported.

## One-time security response after reboot

This private branch also requests `27 01` once during the existing inactive startup stage,
after the inventory matches K7 `YG__ SCC FHCUP`, firmware `1.00 1.02`, part `99110-F6000`
and DID `0142=0002000000`. It rechecks the same stationary gates for every transmission.
It keeps the existing diagnostic session, sends no key or configuration write, then rereads
`0142`. This is separate from the manual `--k7-security-probe` extended-session test.
A rejection only describes the current startup session, not all possible sessions.

`k7-security-probe-startup-v1.claim` prevents another automatic attempt across reboots,
including after an interrupted attempt. Do not delete it to repeatedly probe the ECU.
Skipped stationary/identity gates are included in each boot's inventory report and can be
retried on a later boot. The security result JSON includes NRC/timeout/blocked outcomes,
configuration verification and seed length, without the seed bytes. The existing uploader
sends it without a driving capture and retries network failures. No terminal command or
service restart is required after updating and rebooting in P with radar tracks disabled.

## K7 stationary candidate trial

The separate command `python3 tools/c4_diagnostics/parked_probe.py --candidate-trial` is an explicit,
one-candidate trial from the 6999 terminal. It is not run automatically at ignition or by the
existing read-only Tools button. The launcher requires live P, zero speed, zero engine RPM and
ignition-on state before starting a separate systemd unit. The unit stops comma, checks the exact
K7 radar firmware and original DID `0142=0002000000`, attempts only `0002000001`, then restores
the original configuration. A second independent process checks or restores the original before
comma restarts. Both JSON reports and the status/log tail are queued for the existing uploader.
Do not drive until `/data/c4-diagnostics/k7-parked-probe-status.json` shows `restore_status` as
`confirmed`, `final_config_verified` as `true`, and `comma_restarted` as `true`. A successful write
does not establish radar-track output; a security or condition rejection does not disprove the
candidate value. The next candidate is not selected automatically.
An NRC `0x31` on the write is ambiguous: it does not by itself prove that the candidate bits are wrong.

## K7 bounded security-level survey

The explicit `python3 tools/c4_diagnostics/parked_probe.py --security-survey` command checks
the exact K7 firmware and original `0142=0002000000`, enters extended session, and sends
one seed request each for `27 01`, `27 03`, and `27 05` in the same parked run. Accepted
responses and unsupported `0x12`/`0x31` responses are recorded separately; other errors
stop the survey without further requests.
It records only seed lengths or rejection codes, not seed bytes. The same run compares raw
DTC responses and bounded CAN observations before and after the diagnostic session.
It also captures bounded CAN snapshots before the session, in extended session, after each
requested seed level, and after restoration. These snapshots distinguish observation states;
raw `0x500`-range frame counts alone do not prove usable radar tracks. It sends no key or
configuration write. The existing parked preflight, isolated service,
default-session restoration, original-configuration verification, and automatic report upload
still apply. Review the DAYOU report and `/data/c4-diagnostics/k7-parked-probe-status.json`
before driving. A positive seed response does not authenticate or enable radar tracks.

For one parked visit, `python3 tools/c4_diagnostics/parked_probe.py --batch-survey`
runs the existing default/extended-session characterization and then the bounded security
survey in one isolated comma-stop window. Each step gets its own uploaded JSON report.
The second step is skipped if the first does not verify the original configuration,
default-session restoration, or reports a DTC change. The final status includes
`batch_steps`; check both reports and the comma restart before driving. This batch
contains no key, configuration write, or new radar-track activation attempt.
The status summary includes each step's DID comparison, security responses, and CAN
observations so the two results can be reviewed together without another vehicle run.
After comma restarts, the existing `c4_diagnostics` process uploads both reports and
the status summary to the configured DAYOU endpoint when the network is available.
Failed uploads stay pending and are retried; a queued file alone is not a server receipt.

## K7 engine-off passive CAN address survey

With ignition ON, engine OFF, gear P and the C4 running this branch, use the 6999 terminal.

```sh
cd /data/openpilot
python3 tools/c4_diagnostics/parked_can_survey.py
```

This command checks live vehicle state, receives CAN for 30 seconds without sending any
CAN/UDS request, and stops early if the parked state changes. It lists all bus 1
address/DLC pairs plus `0x500`–`0x53f` candidates on buses 0 and 2, with counts and
per-bit changes. It writes `k7-security-probe-passive-can-*.json` to the existing
automatic-upload spool. A local queued file is not proof of DAYOU receipt. A parked
zero-track result cannot rule out tracks that appear only while driving. The
`0x500` range on source 128 is a send echo, not a received radar track.
