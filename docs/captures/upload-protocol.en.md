[Русский](upload-protocol.md) | **English**

# Uploading gcode to the Centauri Carbon 2 (figured out 20.07.2026)

The one thing the bot couldn't do before: the file had to be put on the printer manually with
the slicer. The protocol was reconstructed by static analysis of the ElegooSlicer web interface
(`C:\Program Files\ElegooSlicer\resources\plugins\elegoolink\web\lan_service_web\index.html`)
and verified with a live upload (6.16 MB in ~1 s).

## Protocol

Not multipart and not POST — **sequential `PUT`s** to the same path, chunk by chunk, with the
offset given by the `Content-Range` header.

```
PUT http://{ip}/upload            (локальная память; /upload/udisk, /upload/sdcard — прочие)
Content-Type:  application/octet-stream
Content-Range: bytes {off}-{end-1}/{total}
X-Token:       {ELEGOO_ACCESS_CODE}
X-File-Name:   {urlencode(имя.gcode)}
X-File-MD5:    {md5 ВСЕГО файла, lowercase hex}
<тело: кусок файла, 1 МиБ>
```

The response to each chunk is JSON: `{"error_code": 0, "offset": <server cursor>}`.
There is no separate "commit": the last chunk completes the upload, and the printer checks the
MD5 itself. After that the file shows up in the method 1044 list (`storage_media: "local"`),
already with parsed metadata (layers, grams, colour/material) — the firmware parses the gcode
itself.

## The main trap: one connection for the whole upload

**All chunks must go over ONE keep-alive connection.** If you open a new TCP connection for each
chunk (as `urllib.request` does), the firmware (`libhv/1.3.4`) responds with
**HTTP 429 Too Many Requests** starting from the 4th chunk, and pauses don't help (I tried up to
1.5 s between chunks — the 429 arrives exactly on the 4th). The browser sends `fetch` over a
single connection, so the web interface doesn't have this problem.

Implementation: one `http.client.HTTPConnection` for the whole upload (`elegoo.upload_gcode`).
With it, 6 MB goes through in ~1 s without a single rejection. If a 429 does arrive anyway, the
connection is reopened after a pause (`UPLOAD_THROTTLE_WAIT`).

Symptom of a wrong implementation: some chunks go through, and at the end you get
`error_code 9004` (`MD5CheckFailed`) — because retries after a 429 append duplicates and the
file on the printer ends up longer than the original.

## Other notes

- **Only `.gcode`** is accepted (the web interface checks the same condition); `.3mf` can't be
  uploaded.
- Chunks are strictly sequential — the server keeps its own cursor (`error_code 9000` =
  FileOffsetMismatch), so they can't be sent in parallel.
- The printer deletes a file with the same name itself before writing (`9010` if it couldn't).
- Error codes: 1000 invalid X-Token, 9000 offset mismatch, 9001/9002/9003 write errors,
  9004 MD5 mismatch, 9007 path does not exist, 9010 couldn't delete the file with the same name,
  9999 other.
- The client has no size limit; we have a 200 MB safety limit on the endpoint.
- `storage_media` in MQTT is hyphenated (`local` / `u-disk` / `sd-card`), while in HTTP paths
  it is written as one word (`/upload`, `/upload/udisk`, `/upload/sdcard`). Two different
  vocabularies, easy to mix up.

## Related MQTT methods (from the same analysis)

- **1047 DeleteFile** — `{"storage_media":"local","file_path":["имя.gcode"]}`. `file_path` is
  an **array**, even for a single file: a string → `error_code 1003` (InvalidParameter). The
  name is taken exactly as in the 1044 list (no directory and no leading slash). Batch deletion
  uses the same array. Not to be confused with history: in 1038 HistoryDelete the field is
  called `list`.
- **2001 LoadFilamenting** — `{"canvas_id":0,"tray_id":slot−1}`, the format is identical to
  2002 (unload). No extra fields; filament type/colour/temperatures are set separately with
  method 2003 SetFilamentInfo. A busy printer returns 1009 PrinterBusy.
- Firmware response codes: 1000 TokenValidationFailed, 1001 UnknownInterface, 1002
  FolderOpenFailed, **1003 InvalidParameter**, 1004 FileWriteFailed, 1005 UpdateTokenFailed,
  1007 FileDeleteFailed, 1008 ResponseDataEmpty, 1009 PrinterBusy.

## Downloading — the companion protocol

See [timelapse-download.en.md](timelapse-download.en.md): `GET /download?X-Token=…&file_name=…`
(it also covers the response with both Content-Length and chunked at the same time, which
aiohttp rejects).
