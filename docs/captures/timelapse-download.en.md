[Русский](timelapse-download.md) | **English**

# Downloading timelapses from the Centauri Carbon 2 (figured out 20.07.2026, updated 24.07.2026)

For a long time "timelapses wouldn't download". There turned out to be three reasons, none of
them obvious.

## MAIN update of 24.07 (task 4c6cd9e8): the mp4 must be BUILT FIRST

A job in the history (1036) has a `time_lapse_video_status` field (an enum from ElegooSlicer's
lan_service_web): **0** = no frames were captured, **1** = frames exist but the mp4 has NOT BEEN
BUILT YET, **2** = the mp4 is ready and downloadable, **3** = the build failed.

While the status is 1, the file DOES NOT EXIST under any name — /download stays silent until
the timeout (the "wrong path" symptom from pitfall #2 below). In this case the slicer sends
**method 1051** ("GetTimeLapseVideoList" — a misleading name, it is actually a BUILD COMMAND):

```json
{"id": N, "method": 1051, "params": {"url": "<time_lapse_video_url задания как есть>"}}
```

The printer renders the video (takes minutes; the slicer waits up to 180 s for the response) and
replies with the ready path for /download (`result.url`); meanwhile the telemetry goes through
subStatus 3020 "building" → 3021 "done" (3022 — failure). Download the returned name VERBATIM,
without rewriting the prefix (the slicer downloads `TimeLapseVideoUrl` as is only when the
status is 2). Implementation: `elegoo.generate_timelapse` + the sweep
`printwatch._tick_timelapse_sweep` (one build per sweep, only while the printer is idle).

It was the unbuilt videos that kept the 21-23.07 journal "without timelapses even though they
exist": the bot was downloading `video/<name>.mp4`, which didn't exist yet, and got endless
silence.

## Working request

```
GET http://{IP}/download?X-Token={ELEGOO_ACCESS_CODE}&file_name={urlencode("video/ИМЯ.mp4")}
```

Example (verified live, 200 OK, 4,839,487 bytes — matches a manual export):

```
http://192.168.1.50/download?X-Token=4ijsD1&file_name=video%2FECC2_0.4_Body1_80%20Elegoo%20PETG%20_0.2_1h38m.gcode20260720215354.mp4
```

`X-Token` is the printer's LAN access code (we already have it in `.env` as
`ELEGOO_ACCESS_CODE`); nothing else needs to be obtained.

## Rule: from the history (MQTT 1036) → to the URL

```python
val  = task["time_lapse_video_url"]      # "picture/ИМЯ"  ЛИБО  "video/ИМЯ.mp4"
name = val.split("/", 1)[1]              # отбрасываем префикс до первого "/"
if not name.endswith(".mp4"):
    name += ".mp4"
file_name = "video/" + name              # префикс ВСЕГДА video/, .mp4 ВСЕГДА
```

**The main trap:** a `picture/` prefix in the 1036 response does not mean "no video was
recorded" — the same video sits in `video/` just fine and can be downloaded. An empty
`time_lapse_video_url` field is what actually means "no video". Implementation:
`webapp._timelapse_file_name/_timelapse_abs_url`.

## Three implementation pitfalls

1. **The response violates the HTTP spec.** The printer (server `libhv/1.3.4`) sends BOTH
   `Content-Length` and `Transfer-Encoding: chunked` at the same time. aiohttp rejects such a
   response (`400 Transfer-Encoding can't be present with Content-Length`) — which is exactly
   why downloading didn't work at all. urllib and browsers read it fine → we download with
   `urllib.request` in `asyncio.to_thread` (`webapp._http_get_bytes`).
2. **A wrong token or path = endless silence.** Not 401 and not 404: the connection is
   accepted, but a response never comes. A timeout is mandatory, otherwise the worker hangs.
3. **Proxy from the environment.** The machine has `HTTP(S)_PROXY` set to a VPN proxy through
   which the printer on the LAN is unreachable. You need `ProxyHandler({})` (urllib) /
   `trust_env=False` (aiohttp).

Other notes: `HEAD` is not supported (404), `Range` is ignored — the file comes only in full.
`Content-Disposition` arrives as `filename="video/ИМЯ.mp4"`, and the browser sanitizes `/` to
`_` — hence the name `video_ИМЯ.mp4` for manually exported files.

## There is no web interface on the printer

A full scan of ports 1–65535: only **53, 80, 1883, 8080, 9001** are open. Port 80 (`libhv`)
answers 404 to everything except `/download`; 8080 is the MJPEG camera (serves the stream on
any path); 9001 is MQTT-over-WebSocket. The "web interface" from which a file is exported
manually is a local page inside ElegooSlicer (`resources/plugins/elegoolink/web/lan_service_web/`),
opened in its WebView2, not a server on the printer.

Sources: [elegoo-homeassistant CC2_PROTOCOL.md](https://github.com/danielcherubini/elegoo-homeassistant/blob/main/docs/CC2_PROTOCOL.md)
(the `file_name` parameter name and the `/download` route with `X-Token` come from there),
[runnane/elegoo-web](https://github.com/runnane/elegoo-web);
the exact values were confirmed by ElegooSlicer debug logs (`%APPDATA%\ElegooSlicer\log\debug_*.log.0`).
