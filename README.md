# homebrew-ip-reporter

A Homebrew tap for **ip-reporter** — a tiny native **Rust** agent that reports a
machine's **WAN IP** and **LAN IP** to the **`ip_info`** gateway on a schedule
(hourly by default). Each host identifies itself by its `hostname`.

The agent is a small (~1 MB) self-contained binary with a near-zero idle
footprint: it's single-threaded, has no async runtime, and sleeps between
reports. TLS is bundled (rustls), so there's no OpenSSL or Python dependency. It
contains no secrets — the auth token is supplied at runtime (CLI flag or config
file).

> Built from a single `src/main.rs` (deps: `ureq` + `gethostname`). A Python
> reference implementation, `ip_reporter.py`, remains in the repo for reference.

## Install

```bash
brew tap guocity/ip-reporter
brew install ip-reporter
```

Or build from source (requires the Rust toolchain):

```bash
cargo build --release   # binary at target/release/ip-reporter
```

## Run as a background service (hourly)

Mint a token on your `ip_info` server host (bound to this machine's hostname),
then install it in one step — the same shape as `cloudflared service install <TOKEN>`:

```bash
# on the server host:
node server/manage-tokens.js generate "$(hostname)" ip_info

# on this machine — saves the token and starts the hourly service:
ip-reporter install --token <TOKEN>
# custom cadence:
ip-reporter install --token <TOKEN> --interval 1800
```

`install` writes the token into `$(brew --prefix)/etc/ip-reporter/config.env` and
runs `brew services restart ip-reporter` for you (start at login, every hour,
across reboots). If you've already put the token in that config, just:

```bash
brew services start ip-reporter
```

Watch it work:
```bash
tail -f "$(brew --prefix)/var/log/ip-reporter.log"
```

## Run directly (no config file)

```bash
ip-reporter --token <TOKEN>              # hourly daemon
ip-reporter --token <TOKEN> --interval 300   # every 5 minutes
ip-reporter --token <TOKEN> --once       # one report, then exit (for cron)
ip-reporter --print                      # dry run: print payload, send nothing
```

## Configuration

Settings resolve as **CLI flag > environment variable > config file > default**.

| Config key / CLI flag        | Env var           | Default                                     |
|------------------------------|-------------------|---------------------------------------------|
| `TELEMETRY_TOKEN` / `--token`| `TELEMETRY_TOKEN` | — (required to actually send)               |
| `TELEMETRY_URL` / `--url`    | `TELEMETRY_URL`   | `https://telemetry.lgnat.com/api/telemetry` |
| `SERVICE_ID` / `--service-id`| `SERVICE_ID`      | (derived from the token)                    |
| `REPORT_INTERVAL` / `--interval` | `REPORT_INTERVAL` | `3600`                                  |

## Uninstall

```bash
brew services stop ip-reporter
brew uninstall ip-reporter
brew untap guocity/ip-reporter
```

## Payload

```json
{ "hostname": "m4airs-MacBook-Air.local", "wan_ip": "71.x.x.x", "lan_ip": "192.168.x.x" }
```

One HTTPS POST to the gateway (`telemetry.lgnat.com`) is validated and bridged
onto the MQTT broker (`mqtt.lgnat.com`) → Telegraf → TimescaleDB.
