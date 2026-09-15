# CYD Focus Dashboard

An always-on homework and personal to-do screen for the ESP32-2432S028R “Cheap Yellow Display” (CYD).

The ESP32 displays upcoming items and refreshes every 15 minutes. A Python bridge, running locally or in Azure Functions, merges incomplete Canvas planner items with personal tasks. The Canvas access token stays in the bridge rather than being copied to the ESP32.

## What is included

- Full-screen 320×240 landscape firmware for the common ESP32-2432S028R
- Swipe-scrollable assignment list with collision-free course-specific colors
- Canvas incomplete-planner sync with pagination and a five-minute cache
- A phone-friendly page for adding and completing personal tasks
- Demo mode, so you can verify the screen before connecting Canvas
- Local bridge with no third-party Python packages, plus an Azure Functions deployment

## 1. Configure the bridge

Install Python 3.11 or newer. From this project folder:

```powershell
Copy-Item bridge/.env.example bridge/.env
python bridge/dashboard_server.py
```

The example configuration starts in demo mode. The terminal prints a LAN URL such as `http://192.168.1.50:8787`; open it on your phone or computer to manage personal tasks, then enter the `DASHBOARD_API_KEY` from `bridge/.env` when prompted.

To connect Canvas, edit `bridge/.env`:

1. Set `CANVAS_BASE_URL` to the address you use for Canvas, such as `https://myschool.instructure.com`.
2. In Canvas, open **Account → Settings → Approved Integrations → New Access Token**. If your school disables personal tokens, its Canvas administrator must provide an approved OAuth route.
3. Paste the token into `CANVAS_TOKEN` and change `DEMO_MODE` to `0`.
4. Change `DASHBOARD_API_KEY` to a long random value.
5. Restart the bridge.

Keep the bridge running for fresh display data. A Raspberry Pi, home server, or always-on computer is ideal later.

## 2. Deploy the bridge to Azure Functions

The cloud implementation uses the Python v2 Functions model and Azure Table
Storage. The HTTP handlers are in `bridge/function_app.py`; personal tasks are
stored by `bridge/table_todo_store.py` instead of on the Function's temporary
filesystem.

### Create the Azure resources

1. In the Azure portal, create a general-purpose v2 Storage account.
2. Create a Function App using **Flex Consumption**, Linux, and Python 3.11.
   Select the Storage account when prompted and enable Application Insights.
3. On the Function App's **Identity** page, enable its system-assigned identity.
4. On the Storage account's **Access control (IAM)** page, grant that identity
   the **Storage Table Data Contributor** role. The app creates the `Todos`
   table on its first data request.
5. Add these Function App environment variables under **Settings → Environment
   variables**:

   | Name | Value |
   | --- | --- |
   | `CANVAS_BASE_URL` | Your Canvas URL, such as `https://myschool.instructure.com` |
   | `CANVAS_TOKEN` | Your Canvas access token |
   | `DASHBOARD_API_KEY` | A long random value shared with the CYD and web page |
   | `DASHBOARD_TIMEZONE` | `America/Denver`, or your IANA time zone |
   | `CANVAS_CACHE_SECONDS` | `300` |
   | `CANVAS_LOOKAHEAD_DAYS` | `45` |
   | `TODO_STORAGE_ENDPOINT` | `https://<storage-name>.table.core.windows.net` |
   | `TODO_TABLE_NAME` | `Todos` |
   | `DEMO_MODE` | `0` |

For local Functions development, install Azurite and Azure Functions Core
Tools, then run:

```powershell
Copy-Item bridge/local.settings.example.json bridge/local.settings.json
python -m pip install -r bridge/requirements.txt
Set-Location bridge
func start
```

After testing, deploy from the `bridge` directory:

```powershell
func azure functionapp publish <app-name>
```

The cloud endpoints are:

- Dashboard: `https://<app-name>.azurewebsites.net/api/dashboard`
- Personal-task page: `https://<app-name>.azurewebsites.net/api/home`
- Health check: `https://<app-name>.azurewebsites.net/api/health`

The personal-task page asks for `DASHBOARD_API_KEY` and saves it only in that
browser tab's session storage. Closing the tab clears it. All dashboard and
task API requests require the key.

### Production security checklist

- Enable **HTTPS Only**, set minimum inbound TLS to 1.2 or newer, and disable
  FTPS on the Function App.
- Set the Storage account's minimum TLS version to 1.2, require secure transfer,
  and keep anonymous blob access disabled.
- Prefer managed identity for `AzureWebJobsStorage` and Table Storage. Keep only
  the documented data-plane roles the Function actually needs.
- Store `CANVAS_TOKEN` and `DASHBOARD_API_KEY` in Azure Key Vault and configure
  versionless Key Vault references in the Function App. Rotate both secrets if
  either has ever been shared or committed.
- Keep the Function publicly reachable only because the CYD needs a public
  endpoint. Every data endpoint still requires `X-API-Key`; the health endpoint
  intentionally reports only `{\"ok\": true}`.
- Do not disable Shared Key access on the deployment Storage account while the
  Flex deployment configuration still uses a storage connection string. First
  migrate deployment storage to managed identity and verify publishing works.

## 3. Configure the CYD

This project uses [PlatformIO](https://platformio.org/) to install the ESP32 framework and display libraries reproducibly.

1. Install VS Code and its PlatformIO extension, or PlatformIO Core.
2. Copy `include/secrets.example.h` to `include/secrets.h`.
3. Enter your Wi-Fi name and password.
4. For Azure, set `DASHBOARD_URL` to
   `https://<app-name>.azurewebsites.net/api/dashboard`. For the local bridge,
   use the LAN endpoint it prints. Do not use `localhost`.
   Azure's DigiCert root CA is pinned in `include/azure_root_ca.h`; if you use a
   different HTTPS provider, define `DASHBOARD_ROOT_CA` in `include/secrets.h`.
   Plain HTTP is rejected by default. For temporary local development on a
   trusted LAN, define `ALLOW_INSECURE_HTTP 1` in `include/secrets.h`; be aware
   that this sends the dashboard key and data without transport encryption.
5. Set `DASHBOARD_API_KEY` to the value used in `bridge/.env` or the Function
   App environment variables.
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
- **Local bridge unavailable:** both devices must be on the same network. Allow Python through Windows Firewall on private networks and confirm the computer’s LAN address did not change.
- **Azure returns 401:** make sure the firmware and web page use the exact `DASHBOARD_API_KEY` configured on the Function App.
- **Azure returns 503:** verify the Table Storage endpoint, managed-identity role, and Canvas environment variables. A new role assignment can take several minutes to propagate.
- **Canvas returns 401:** recreate the token and check the Canvas base URL. Never commit `.env` or `include/secrets.h`.
- **Assignment missing:** the bridge asks Canvas for incomplete planner items over the next 45 days. Check that it appears in Canvas’s To Do/Planner.

## Verify the bridge

```powershell
python -m unittest discover -s tests -v
Invoke-RestMethod http://localhost:8787/health
```

To verify the Azure Function, use:

```powershell
$headers = @{ "X-API-Key" = "<dashboard-api-key>" }
Invoke-RestMethod "https://<app-name>.azurewebsites.net/api/dashboard" -Headers $headers
```

## Hardware assumptions

The target is the original ESP32-2432S028R with an ILI9341-compatible 320×240 display. The configuration uses Espressif’s published Arduino variant pins: TFT MOSI 13, MISO 12, clock 14, CS 15, DC 2, and backlight 21.

Touch scrolling uses the board's XPT2046 controller on MOSI 32, MISO 39,
clock 25, CS 33, and IRQ 36. These pins match the original 2.8-inch resistive
touch CYD; similar-looking display variants can use different controllers or
pinouts.
