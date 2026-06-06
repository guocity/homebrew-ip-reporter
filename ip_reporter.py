#!/usr/bin/env python3
"""
ip_reporter.py — report this machine's WAN and LAN IP to the telemetry gateway.

Standalone: uses only the Python 3 standard library (no pip installs), so it
can be dropped onto any macOS/Linux host and run directly or as a service.

Each run (or each interval tick) it:
  1. Reads the machine hostname (e.g. "m4airs-MacBook-Air.local").
  2. Discovers the primary LAN IPv4 address.
  3. Looks up the public WAN IP via an external service.
  4. POSTs a small JSON payload to the telemetry gateway with a Bearer token.

The gateway (the Cloudflare Worker at telemetry.lgnat.com) validates the token,
derives the service_id from it, and bridges the payload onto the MQTT broker
(mqtt.lgnat.com) -> Telegraf -> TimescaleDB. So one HTTP call lands the data on
both telemetry.lgnat.com and mqtt.lgnat.com.

The token can be passed directly with --token (no config file needed). Settings
resolve with the precedence:  CLI flag > environment variable > config file >
built-in default. The optional config file is a simple KEY=VALUE file (see
config.example.env); recognized keys are TELEMETRY_TOKEN, TELEMETRY_URL,
SERVICE_ID, and REPORT_INTERVAL.

By default it runs forever, reporting every hour (--interval 3600). Use --once
for a single report (e.g. under cron) and --print for a dry run.

    # Direct, token on the command line, hourly:
    ./ip_reporter.py --token <TOKEN>

    # Every 5 minutes:
    ./ip_reporter.py --token <TOKEN> --interval 300

    # One-shot, no send (just show what would be sent):
    ./ip_reporter.py --print

Token generation (on the server host), bound to this machine's hostname:

    cd server && node manage-tokens.js generate "$(hostname)" telemetry
"""

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TELEMETRY_URL = "https://telemetry.lgnat.com/api/telemetry"
DEFAULT_INTERVAL = 3600  # seconds (1 hour)
HTTP_TIMEOUT = 10  # seconds

# Services that echo back the caller's public IP as plain text.
WAN_IP_PROVIDERS = (
    "https://cloudflare.com/cdn-cgi/trace",  # parsed specially (ip=...)
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
)

# Config file search order (first existing wins) when --config is not given.
# A config file is entirely optional.
DEFAULT_CONFIG_PATHS = (
    os.environ.get("IP_REPORTER_CONFIG"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env"),
    os.path.expanduser("~/.config/ip-reporter/config.env"),
    "/opt/homebrew/etc/ip-reporter/config.env",
    "/usr/local/etc/ip-reporter/config.env",
)


def log(msg):
    """Timestamped line to stderr (service managers capture this to a log file)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{stamp}] {msg}", file=sys.stderr, flush=True)


def read_config_file(explicit_path=None):
    """Parse an optional KEY=VALUE config file. Returns {} if none is found."""
    paths = (explicit_path,) if explicit_path else DEFAULT_CONFIG_PATHS
    cfg = {}
    for path in paths:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    cfg[key.strip()] = value.strip().strip('"').strip("'")
            log(f"Loaded config from {path}")
            break
    return cfg


def resolve(cli_value, env_key, cfg, cfg_key, default=None):
    """Resolve one setting: CLI flag > env var > config file > default."""
    if cli_value not in (None, ""):
        return cli_value
    if os.environ.get(env_key):
        return os.environ[env_key]
    if cfg.get(cfg_key):
        return cfg[cfg_key]
    return default


def get_hostname():
    """The machine hostname, e.g. 'MacBook-Air'."""
    return socket.gethostname()


def get_lan_ip():
    """Primary outbound LAN IPv4 address.

    Opens a UDP socket toward a public address and inspects the local end of
    the route. No packets are actually sent, so it is fast and dependency-free.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
    finally:
        sock.close()


def get_wan_ip():
    """Public WAN IP via the first provider that responds."""
    ctx = ssl.create_default_context()
    for url in WAN_IP_PROVIDERS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ip-reporter/1.0"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
            if "cdn-cgi/trace" in url:
                for line in body.splitlines():
                    if line.startswith("ip="):
                        return line[3:].strip()
                continue
            # Plain-text providers return just the IP.
            ip = body.split()[0] if body else ""
            if ip:
                return ip
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"WAN lookup via {url} failed: {exc}")
            continue
    return None


def build_payload(service_id=None):
    payload = {
        "hostname": get_hostname(),
        "wan_ip": get_wan_ip(),
        "lan_ip": get_lan_ip(),
    }
    if service_id:
        payload["service_id"] = service_id
    return payload


def send(url, token, payload):
    """POST the payload to the telemetry gateway with a Bearer token."""
    if not token:
        log("ERROR: no token. Pass --token, set TELEMETRY_TOKEN, or add it to a config file.")
        return False

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ip-reporter/1.0",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
            log(f"Sent OK ({resp.status}): {body}")
            return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        log(f"ERROR: gateway returned HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, OSError) as exc:
        log(f"ERROR: could not reach gateway {url}: {exc}")
    return False


def report_once(url, token, service_id, dry_run=False):
    payload = build_payload(service_id)
    log(f"Payload: {json.dumps(payload)}")
    if dry_run:
        return True
    return send(url, token, payload)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report this machine's WAN/LAN IP to the telemetry gateway.",
    )
    parser.add_argument("--token", help="Bearer token (overrides env/config).")
    parser.add_argument("--url", help=f"Gateway URL (default: {DEFAULT_TELEMETRY_URL}).")
    parser.add_argument("--service-id", dest="service_id",
                        help="Optional explicit service_id field in the payload.")
    parser.add_argument("--interval", type=int, default=None, metavar="SECONDS",
                        help=f"Seconds between reports (default: {DEFAULT_INTERVAL}). "
                             "Ignored when --once is given.")
    parser.add_argument("--once", action="store_true",
                        help="Report a single time and exit (e.g. under cron).")
    parser.add_argument("--print", dest="dry_run", action="store_true",
                        help="Collect and print the payload without sending it (implies --once).")
    parser.add_argument("--config", help="Explicit path to a KEY=VALUE config file.")
    args = parser.parse_args(argv)

    cfg = read_config_file(args.config)

    token = resolve(args.token, "TELEMETRY_TOKEN", cfg, "TELEMETRY_TOKEN")
    url = resolve(args.url, "TELEMETRY_URL", cfg, "TELEMETRY_URL", DEFAULT_TELEMETRY_URL)
    service_id = resolve(args.service_id, "SERVICE_ID", cfg, "SERVICE_ID")
    interval = int(resolve(
        args.interval, "REPORT_INTERVAL", cfg, "REPORT_INTERVAL", DEFAULT_INTERVAL))

    # A single shot: --once or a dry run.
    if args.once or args.dry_run:
        return 0 if report_once(url, token, service_id, dry_run=args.dry_run) else 1

    # Daemon mode (default): report immediately, then every `interval` seconds.
    log(f"ip-reporter started: reporting every {interval}s to {url}")
    while True:
        report_once(url, token, service_id)
        time.sleep(max(1, interval))


if __name__ == "__main__":
    sys.exit(main())
