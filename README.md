# LG Soundbars Health

A Home Assistant helper integration for the built-in LG Soundbars integration.

It adds diagnostic entities that track whether the soundbar is reachable over TCP,
whether the DNS-resolved IP changed, whether the original IP is still reachable,
and checks replies through the native LG connection. It can optionally reload the
parent LG Soundbars config entry after IP drift or an unresponsive native connection.

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

- Connection (TCP reachability)
- Integration connection (native LG status replies)
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

When enabled, recovery requires current TCP reachability and either:
- the IP changed and the initial IP failed at least three consecutive checks, or
- the native LG connection failed three consecutive heartbeat checks.

Reloads are separated by a minimum 10-minute cooldown. The heartbeat uses a
read-only status query through the native connection and waits up to five
seconds for its reply. A normal powered-off response is healthy. It never sends
power, volume, input or playback commands.

The native connection adapter depends on the built-in LG integration and
`temescal` internals. Unsupported interfaces, disabled entities and integrations
that are not loaded are reported as unknown and do not trigger heartbeat
recovery. Existing TCP diagnostics retain their original meaning. The native
media player's availability behavior is unchanged; use Integration connection
to distinguish a stale media-player state from a working connection.

Diagnostic attributes include `parent_connected`, `parent_failure_count`,
`parent_last_success`, `parent_last_error` and `auto_reload_reason`.

## Tests

Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`.
Tests use local socket pairs and the actual coordinator methods with Home
Assistant framework boundaries substituted; no physical device is required.
