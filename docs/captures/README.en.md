[Русский](README.md) | **English**

# MQTT traffic captures from the printer (Centauri Carbon 2)

Raw sniffing logs from the printer's broker (`<ip>:1883`, subscription `elegoo/#`). They used to
live in temporary Claude session folders and got lost on cleanup — now they are here. The
sniffing method is described in `docs/Уже в продукте/printer-integration.md`, and the key
breakdown is in `inventory_master_plan.md` §5.1.1 and `printer-actions-audit.md`.

## Files

| File | What it is |
|---|---|
| `mqtt-capture-2026-07-12-full.log` | **Main capture of 12.07** (100 s): PING/PONG heartbeat, full responses to 1036 (history), 2005 (slots), 1044 (files, local + u-disk err 1017), 1002 (full status), 1001 (info/firmware), 1042 (camera URL) + a frame summary |
| `mqtt-capture-2026-07-12-partial.log` | First run of the same sniffer: a burst of ElegooSlicer requests on opening the Device screen; it crashed on cp1251 Unicode (CJK characters in a file name) — hence "partial" |
| `mqtt-harvest-check-2026-07-12.log` | Check of passive harvesting: 18 api_response in 90 s → harvest files=20, history=44 |
| `broadcast-idle-2026-07-20.json` | Snapshot of the bot's background subscriber cache while IDLE: the api_status broadcast carries temps + **canvas_info (slots)** + **ztemperature_sensor (chamber)** with no binding at all |
| `mqtt-register-binding-2026-07-20.log` | **BINDING RECON (751 lines):** full `api_register`→`register_response` payload + all ElegooSlicer commands with `error_code:0` responses — light 1029, fans 1030, home 1026, jog 1027. Based on this log the bot became a "bound" client itself |

## ⭐ Binding FIGURED OUT (20.07.2026) — the bot is now a full client

From `mqtt-register-binding-2026-07-20.log`. The "binding" mechanism that made the printer send
unicast responses only to the slicer:

```
→  elegoo/{SN}/api_register              {"request_id":"0cliaa2497","client_id":"0cliaa2497"}
←  elegoo/{SN}/0cliaa2497/register_response   {"client_id":"0cliaa2497","error":"ok"}
```

After that the client sends requests to `elegoo/{SN}/{client_id}/api_request`, and the printer
responds unicast to `.../api_response`. The binding is kept alive by a heartbeat PING
`{"type":"PING"}` on the client's own channel roughly every 8–10 s. A repeated `api_register` with
the same `client_id` → `"error":"already registered"` (also a success).

**Verified live (probe + bot):** after registration the printer answered the bot on
1044/1002/2005/1036, and command 1029 (light) returned `error_code:0` — a confirmed success.
Implemented in `app/elegoo.py` (CHANNEL_ID, `_register`, `_bound`, bound `request()`).

**Command codes from this capture (`error_code:0` response to each):**

| Action | method | params |
|---|---|---|
| Light on/off | 1029 | `{"power":1\|0}` (method has NOT changed since 04.06) |
| Fan | 1030 | `{"fan"\|"aux_fan"\|"box_fan": 0..255}` (0–255 scale, NOT a fraction!) |
| Home axes | 1026 | `{"homed_axes":"xyz"}` |
| Move axis (jog) | 1027 | `{"axes":"z"\|"x"\|"y","distance":±N}` |

> Note on fans: command 1030 uses a 0–255 scale, while in the `fans.*.speed` telemetry the
> user fans (model/aux/box) come as a 0..1 fraction and the system fans (controller/heater) as
> 0..255. `elegoo._fan_pct` normalizes both (threshold 1.5).

## Key facts from the captures

- Format: JSON-RPC over MQTT, topics `elegoo/{SN}/{client}/api_request|api_response`,
  broadcast `.../api_status` (method 6000, roughly every 7 s).
- ElegooSlicer keeps a persistent channel `1_PC_2769` (PING/PONG every ~10 s) and, each time
  the printer screen is opened, creates a one-off channel `0cli<hex>` for a burst of requests.
- **`api_register` → `register_response`** is the client "binding". On 12.07 it was only visible
  in the frame summary (payload lost); on 20.07 the payload was captured in full (see the ⭐
  section above) and built into the bot — the bot is now a full client and doesn't depend on
  the slicer being open.
- Idle broadcast (20.07): `extruder.temperature`, `heater_bed.temperature`,
  `ztemperature_sensor.temperature`, `canvas_info.canvas_list[].tray_list[]` (brand/type/colour/
  temperatures per slot). Everything else (`print_status`, `fans`, `gcode_move`, `led`,
  `machine_status`) arrives as deltas during printing/on changes — see the full structure in the
  1002 response inside the full log.
