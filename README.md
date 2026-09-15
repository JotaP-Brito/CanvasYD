# CYD Focus Dashboard

An always-on homework and personal to-do screen for the ESP32-2432S028R “Cheap Yellow Display” (CYD).

The ESP32 displays the next four items and refreshes once a minute. A small Python bridge on your computer merges incomplete Canvas planner items with personal tasks. The Canvas access token stays on the computer rather than being copied to the ESP32.

## What is included

- 320×240 landscape firmware for the common ESP32-2432S028R
- Canvas incomplete-planner sync with pagination and a five-minute cache
- A phone-friendly page for adding and completing personal tasks
- Demo mode, so you can verify the screen before connecting Canvas
- No third-party Python packages

## 1. Configure the bridge

Install Python 3.11 or newer. From this project folder:

```powershell
Copy-Item bridge/.env.example bridge/.env
python bridge/dashboard_server.py
```

The example configuration starts in demo mode. The terminal prints a LAN URL such as `http://192.168.1.50:8787`; open it on your phone or computer to manage personal tasks.

To connect Canvas, edit `bridge/.env`:

1. Set `CANVAS_BASE_URL` to the address you use for Canvas, such as `https://myschool.instructure.com`.
2. In Canvas, open **Account → Settings → Approved Integrations → New Access Token**. If your school disables personal tokens, its Canvas administrator must provide an approved OAuth route.
3. Paste the token into `CANVAS_TOKEN` and change `DEMO_MODE` to `0`.
4. Change `DASHBOARD_API_KEY` to a long random value.
5. Restart the bridge.

Keep the bridge running for fresh display data. A Raspberry Pi, home server, or always-on computer is ideal later.

## 2. Configure the CYD

This project uses [PlatformIO](https://platformio.org/) to install the ESP32 framework and display libraries reproducibly.

1. Install VS Code and its PlatformIO extension, or PlatformIO Core.
2. Copy `include/secrets.example.h` to `include/secrets.h`.
3. Enter your Wi-Fi name and password.
4. Set `DASHBOARD_URL` to the device endpoint printed by the bridge. Do not use `localhost`.
5. Set `DASHBOARD_API_KEY` to the value used in `bridge/.env`.
6. Build the firmware with **PlatformIO: Build**. The application binary is
   generated at `.pio/build/cyd/firmware.bin`.
7. To preserve an existing [Launcher](https://github.com/bmorcelli/Launcher)
   installation, install `firmware.bin` from Launcher's **WUI** or **SD** menu.
   Do not use PlatformIO's Upload action in this workflow because a normal USB
   upload can replace Launcher's bootloader and partition layout.

Command-line users can run:

```powershell
pio run
```

If you intentionally want the dashboard to replace Launcher, connect the CYD
over USB and run `pio run --target upload`. You can then use
`pio device monitor` for serial diagnostics.

## Troubleshooting

- **Blank screen:** verify the board is the 2.8-inch ESP32-2432S028R. Similar-looking variants use different drivers and pins.
- **Wrong colors:** swap `TFT_INVERSION_ON` for `TFT_INVERSION_OFF` in `platformio.ini`.
- **Bridge unavailable:** both devices must be on the same network. Allow Python through Windows Firewall on private networks and confirm the computer’s LAN address did not change.
- **Canvas returns 401:** recreate the token and check the Canvas base URL. Never commit `.env` or `include/secrets.h`.
- **Assignment missing:** the bridge asks Canvas for incomplete planner items over the next 45 days. Check that it appears in Canvas’s To Do/Planner.

## Verify the bridge

```powershell
python -m unittest discover -s tests -v
Invoke-RestMethod http://localhost:8787/health
```

## Hardware assumptions

The target is the original ESP32-2432S028R with an ILI9341-compatible 320×240 display. The configuration uses Espressif’s published Arduino variant pins: TFT MOSI 13, MISO 12, clock 14, CS 15, DC 2, and backlight 21.
