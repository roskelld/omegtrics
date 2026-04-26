# Omegtrics Firefox Extension

This is a temporary/test WebExtension that runs directly on SteamDB app chart pages.

## Install for local testing

1. Open Firefox.
2. Go to `about:debugging#/runtime/this-firefox`.
3. Click **Load Temporary Add-on...**.
4. Select `firefox-extension/manifest.json`.
5. Open a SteamDB app charts page, for example:
   `https://steamdb.info/app/3932890/charts/`

The extension waits for the rendered SteamDB Highcharts player chart, extracts the visible CCU series from the DOM, estimates DAU, and injects an Omegtrics panel directly below the player chart.

## Notes

- The extension does not fetch SteamDB data itself.
- It only analyses a page the user has already loaded in Firefox.
- DAU is estimated from player-hours and an assumed average session length. The panel exposes low, midpoint, and high session-hour assumptions so the user can test the sensitivity range.
