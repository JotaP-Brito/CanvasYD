#pragma once

// Copy this file to secrets.h and fill in your own values.
// secrets.h is ignored by Git.
#define WIFI_SSID "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

// For Azure Functions, use https://<app-name>.azurewebsites.net/api/dashboard.
// For the local bridge, use its LAN address. Never use localhost here:
// localhost on the CYD means the CYD itself.
#define DASHBOARD_URL "https://your-app.azurewebsites.net/api/dashboard"
#define DASHBOARD_API_KEY "replace-with-the-same-key-as-bridge-env"

// Plain HTTP is blocked by default because it exposes the API key and dashboard
// data on the network. For short-lived local development on a trusted LAN only:
// #define ALLOW_INSECURE_HTTP 1

// Azure endpoints use the verified root in azure_root_ca.h. For another HTTPS
// provider, override it with that provider's trusted PEM root certificate.
// #define DASHBOARD_ROOT_CA "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"
