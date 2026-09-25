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
one `27 03` seed request. Only if that request returns unsupported `0x12` or `0x31`, it
sends one `27 05` request in the same parked run. Other responses stop the survey.
It records only seed lengths or rejection codes, not seed bytes. The same run compares raw
DTC responses and bounded CAN observations before and after the diagnostic session.
It sends no key or configuration write. The existing parked preflight, isolated service,
default-session restoration, original-configuration verification, and automatic report upload
still apply. Review the DAYOU report and `/data/c4-diagnostics/k7-parked-probe-status.json`
before driving. A positive seed response does not authenticate or enable radar tracks.
