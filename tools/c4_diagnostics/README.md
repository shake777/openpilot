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
