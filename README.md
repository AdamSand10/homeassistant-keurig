# Keurig Connect for Home Assistant

Home Assistant custom integration for Keurig **SMART** brewers (K-Supreme SMART, K-Supreme Plus SMART, K-Café SMART) through the Keurig cloud.

## Why this fork exists

The original integration by [@dcmeglio](https://github.com/dcmeglio) stopped installing because its library, `pykeurig`, was removed from PyPI, so setup failed with a "500 error". This fork:

- **Bundles `pykeurig`** inside the integration (from [arketec/pykeurig](https://github.com/arketec/pykeurig) 1.4.4.3, Oct 2026, which includes the updated Keurig subscription key and token-refresh fix), so nothing has to be downloaded from PyPI.
- Updates the code for current Home Assistant: modern config flow with unique ID and proper re-authentication, `DeviceInfo` import path, coordinator `config_entry`, views registered once, non-blocking disconnect.

## Install (HACS)

1. HACS → ⋮ → Custom repositories → add `https://github.com/AdamSand10/homeassistant-keurig` as **Integration**.
2. Download **Keurig Connect**, restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **Keurig Connect**, then follow the sign-in steps below.

## Signing in

Keurig no longer accepts username/password logins from third-party clients (the old token endpoint is blocked by Cloudflare, and Auth0 bot detection rejects password grants). Sign-in is now done once in a browser:

1. Open your browser's developer tools on the **Network** tab and enable **Preserve log**.
2. Click the **Sign in to Keurig** link in the setup dialog and sign in normally (including any CAPTCHA).
3. Find the request to the keurig.com redirect address that contains `?code=`, copy its full URL, and paste it into the dialog within about a minute.

After that the integration runs unattended on rotating refresh tokens, saved to the config entry. If the session ever ends, Home Assistant asks you to repeat the sign-in. Existing installs are prompted to re-authenticate once after updating; the stored password is removed when they do.

Status is polled every 60 seconds (real-time push is disabled).

## Entities

- `sensor.<brewer>_brewer_status`: ready, brewing, complete, no water, pod not removed, lid open, canceling
- `sensor.<brewer>_pod_status`: empty, present, used, bad image, with pod brand, variety and roast attributes
- `switch.<brewer>_power`

## Services

`keurig.brew_hot`, `keurig.brew_hot_water`, `keurig.brew_iced`, `keurig.brew_recommendation`, `keurig.brew_favorite`, `keurig.cancel_brew`, `keurig.add_favorite`, `keurig.update_favorite`, `keurig.delete_favorite`.

## Credits

Original integration and library by Dominick Meglio (@dcmeglio, MIT). Library maintenance by @jspeckman and @arketec. Keurig's API is undocumented and may change at any time.
