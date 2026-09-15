#pragma once

// Copy this file to secrets.h and fill in your own values.
// secrets.h is ignored by Git.
#define WIFI_SSID "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

// Use the LAN address printed when bridge/dashboard_server.py starts.
// Do not use localhost here: localhost on the CYD means the CYD itself.
#define DASHBOARD_URL "http://192.168.1.50:8787/api/dashboard"
#define DASHBOARD_API_KEY "replace-with-the-same-key-as-bridge-env"

