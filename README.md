# LG Soundbars Health

A Home Assistant helper integration for the built-in LG Soundbars integration.

It adds diagnostic entities that track whether the soundbar is reachable over TCP,
whether the DNS-resolved IP changed, whether the original IP is still reachable,
and can optionally reload the parent LG Soundbars config entry when IP drift is detected.

## Requirements

- Home Assistant
- Built-in LG Soundbars integration configured first
- HACS or manual custom_components install

## Installation via HACS custom repository

1. HACS → three dots → Custom repositories
2. Add this repository URL
3. Category: Integration
4. Install LG Soundbars Health
5. Restart Home Assistant
6. Settings → Devices & services → Add integration → LG Soundbars Health

## Entities

- Connection
- Initial IP connection
- IP changed
- Resolved IP
- Initial IP
- Response time
- Last seen
- Offline duration
- Failure count
- Reload LG Soundbar
- Auto reload LG Soundbar

## Auto reload

Disabled by default.

When enabled, it reloads the parent LG Soundbars config entry only if:
- current resolved IP is reachable
- initial IP is not reachable
- IP changed
- initial IP failed at least 3 consecutive checks
- cooldown passed