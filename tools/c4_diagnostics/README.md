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

On a device, a valid private configuration enables capture whenever this branch's on-road service runs. The service captures, from radar bus 1 only, the classic object triplets `0x238` through `0x255` and the other radar-bus messages under analysis (`0x201`–`0x20d`, `0x25a`–`0x25e`, `0x266`–`0x26f`, `0x690`, `0x691`); the same IDs on bus 0 are unrelated frames and are skipped. It also captures classical CAN addresses `0x500` through `0x53f`, the SCC addresses `0x389`, `0x420`, `0x421`, and `0x50a`. It does not send CAN messages or change driving control.

Raw bytes, bus and timestamps use the `C4RADAR1` format. Use `decode.py` to inspect them; the scene track count is not a diagnostic success indicator. This uploader remains specific to `carrot-wip-custom`.

Radar CAN, synchronized scene data, and the receive-only CAN inventory are collected in bounded bundles. Periodic bundles start at the beginning of each drive and every 10 minutes of continuous on-road time; each starts with a 30-second qRoad H.264 preview and lasts up to about 60 seconds (usually about 40 seconds, about 4.6 MB). Between them, the service keeps the last 15 seconds of radar CAN and scene frames in memory and writes an event bundle (no video, about 1.5 MB) with those 15 seconds plus 5 more when a review event occurs: a driver brake press while openpilot longitudinal control is active, a commanded acceleration of -2.0 m/s^2 or less, or a stop behind a lead within 15 seconds of driving at 20 km/h or faster. Events are at least 2 minutes apart and at most 10 per hour; an event during a periodic bundle only labels that bundle. Each event bundle carries a `.c4event.json` companion with the reason. Expected load is about 28-43 MB per driving hour. Time outside bundles is not recorded. The local spool is kept under 512 MB: when it is larger, whole bundles are deleted oldest first, already-uploaded bundles before ones still waiting for upload. Each upload includes the original radar CAN capture, a bounded `.c4scene` JSON Lines companion with synchronized `liveTracks`, model path, lane, radar lead, speed, and longitudinal-control state, a bounded `.c4can.json` receive-only inventory, an optional qRoad preview with timing metadata, and a small `/proc/meminfo` snapshot. The inventory records address, bus, length, count, approximate rate, first/last payload, and the varying-bit mask for incoming CAN only; it does not transmit frames or retain every full payload. Video is copied from the existing 256 kbps qRoad encoder and does not start another camera or encoder. Collection occurs only while the car is on-road; the bounded diagnostic buffer described above may include preceding off-road frames. Completed captures upload whenever `deviceState` reports a network connection, including after the car goes off-road. Failed uploads retain the same deterministic UUID and are retried without deleting the local originals. Incomplete radar-only fragments are preserved locally and are not uploaded.

There is no time limit. Capturing and uploading stop when this branch's service is no longer running, such as after switching the device back to a branch without the C4 diagnostics process.

The parked K7 probes, security/seed surveys, candidate trial and startup radar
inventory were removed on 2026-10-03; only the automatic driving capture above
remains.
