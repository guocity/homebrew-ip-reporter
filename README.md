# homebrew-ip-reporter

A Homebrew tap for **ip-reporter** — a tiny, dependency-free Python agent that
reports a machine's **WAN IP** and **LAN IP** to a telemetry gateway on a
schedule (hourly by default). Each host identifies itself by its `hostname`.

The agent uses only the Python 3 standard library and contains no secrets — the
auth token is supplied at runtime (CLI flag or config file).

## Install

```bash
brew tap guocity/ip-reporter
brew install ip-reporter
```

## Run as a background service (hourly)

Mint a token on your telemetry server host (bound to this machine's hostname),
then install it in one step — the same shape as `cloudflared service install <TOKEN>`:

```bash
# on the server host:
node server/manage-tokens.js generate "$(hostname)" telemetry

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
