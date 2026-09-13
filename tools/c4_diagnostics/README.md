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
