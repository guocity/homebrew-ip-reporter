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
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

__version__ = "1.0.3"
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
    """Parse an optional KEY=VALUE config file. Returns (cfg, loaded_path)."""
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
            return cfg, path
    return cfg, None


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


def build_payload(wan_ip, lan_ip, service_id=None):
    payload = {
        # service_id (derived from the token) is this machine's hostname, so the
        # hostname field is redundant — commented out for now.
        # "hostname": get_hostname(),
        "wan_ip": wan_ip,
        "lan_ip": lan_ip,
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


def check_and_report(url, token, service_id, write_path, last_ips, dry_run=False):
    wan_ip = get_wan_ip()
    lan_ip = get_lan_ip()

    if dry_run:
        payload = build_payload(wan_ip, lan_ip, service_id)
        log(f"Payload: {json.dumps(payload)}")
        return True

    last_wan = last_ips.get("last_wan_ip")
    last_lan = last_ips.get("last_lan_ip")

    # Compare current resolved IP with cached IP. If resolved IP is None (glitch), we don't count it as a change.
    ip_changed = False
    if wan_ip is not None:
        if last_wan is None or wan_ip != last_wan:
            ip_changed = True
    if lan_ip is not None:
        if last_lan is None or lan_ip != last_lan:
            ip_changed = True

    if not ip_changed:
        log(f"IPs unchanged (WAN: {wan_ip or 'unknown'}, LAN: {lan_ip or 'unknown'}). Skipping send.")
        return True

    payload = build_payload(wan_ip, lan_ip, service_id)
    log(f"Payload: {json.dumps(payload)}")

    if send(url, token, payload):
        if wan_ip is not None:
            last_ips["last_wan_ip"] = wan_ip
        if lan_ip is not None:
            last_ips["last_lan_ip"] = lan_ip

        values = {}
        if last_ips.get("last_wan_ip"):
            values["LAST_WAN_IP"] = last_ips["last_wan_ip"]
        if last_ips.get("last_lan_ip"):
            values["LAST_LAN_IP"] = last_ips["last_lan_ip"]

        if values:
            try:
                write_config_values(write_path, values)
            except Exception as exc:
                log(f"WARNING: could not save updated IPs to config {write_path}: {exc}")
        return True
    else:
        return False


# --- `install` subcommand: bake the token into config + (re)start the service ---
# This mirrors `cloudflared service install <TOKEN>`: one command sets the token
# and brings the background service up.

def resolve_writable_config(explicit=None):
    """Pick the config file the service reads, creating its path if needed.

    Order: --config > IP_REPORTER_CONFIG > an existing standard file (incl. the
    Homebrew etc that `brew services` reads) > the Homebrew etc path if brew is
    present > ~/.config/ip-reporter/config.env.
    """
    if explicit:
        return explicit
    if os.environ.get("IP_REPORTER_CONFIG"):
        return os.environ["IP_REPORTER_CONFIG"]

    candidates = (
        "/opt/homebrew/etc/ip-reporter/config.env",
        "/usr/local/etc/ip-reporter/config.env",
        os.path.expanduser("~/.config/ip-reporter/config.env"),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path

    try:
        prefix = subprocess.check_output(
            ["brew", "--prefix"], text=True, stderr=subprocess.DEVNULL).strip()
        if prefix:
            return os.path.join(prefix, "etc", "ip-reporter", "config.env")
    except (OSError, subprocess.SubprocessError):
        pass

    return os.path.expanduser("~/.config/ip-reporter/config.env")


def write_config_values(path, values):
    """Upsert KEY=VALUE pairs into a config file, preserving other lines."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    remaining = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key in remaining and not stripped.startswith("#"):
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)


def do_install(argv):
    parser = argparse.ArgumentParser(
        prog="ip-reporter install",
        description="Save the token into the service config and (re)start the hourly "
                    "service — like `cloudflared service install <TOKEN>`.",
    )
    parser.add_argument("token_pos", nargs="?", metavar="TOKEN",
                        help="Bearer token (may also be given with --token).")
    parser.add_argument("--token", help="Bearer token.")
    parser.add_argument("--url", help="Override the gateway URL.")
    parser.add_argument("--interval", type=int, metavar="SECONDS",
                        help=f"Report cadence in seconds (default: {DEFAULT_INTERVAL}).")
    parser.add_argument("--config", help="Explicit config file path to write.")
    parser.add_argument("--no-start", action="store_true",
                        help="Write the config but do not start the service.")
    args = parser.parse_args(argv)

    token = args.token or args.token_pos
    if not token:
        parser.error("a token is required (positional TOKEN or --token)")

    target = resolve_writable_config(args.config)
    values = {"TELEMETRY_TOKEN": token}
    if args.url:
        values["TELEMETRY_URL"] = args.url
    if args.interval:
        values["REPORT_INTERVAL"] = str(args.interval)

    write_config_values(target, values)
    log(f"Saved token to {target}")

    if args.no_start:
        log("Config written. Start it with: brew services start ip-reporter")
        return 0

    if not shutil.which("brew"):
        log("Homebrew not found. Config written; start the service however you "
            "manage it (e.g. ./install.sh, or run: ip-reporter).")
        return 0

    log("Starting service: brew services restart ip-reporter")
    rc = subprocess.call(["brew", "services", "restart", "ip-reporter"])
    if rc == 0:
        log("Service running. Logs: $(brew --prefix)/var/log/ip-reporter.log")
    return rc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # Subcommand: `ip-reporter install [TOKEN] [--token ...]`
    if argv and argv[0] == "install":
        return do_install(argv[1:])

    parser = argparse.ArgumentParser(
        description="Report this machine's WAN/LAN IP to the telemetry gateway.",
        epilog="Subcommand: `ip-reporter install --token <TOKEN>` saves the token "
               "and starts the hourly service (cloudflared-style).",
    )
    parser.add_argument("-v", "--version", action="version",
                        version=f"ip-reporter {__version__} (python)")
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

    cfg, config_path = read_config_file(args.config)

    token = resolve(args.token, "TELEMETRY_TOKEN", cfg, "TELEMETRY_TOKEN")
    url = resolve(args.url, "TELEMETRY_URL", cfg, "TELEMETRY_URL", DEFAULT_TELEMETRY_URL)
    service_id = resolve(args.service_id, "SERVICE_ID", cfg, "SERVICE_ID")
    interval = int(resolve(
        args.interval, "REPORT_INTERVAL", cfg, "REPORT_INTERVAL", DEFAULT_INTERVAL))

    last_ips = {
        "last_wan_ip": cfg.get("LAST_WAN_IP"),
        "last_lan_ip": cfg.get("LAST_LAN_IP"),
    }
    write_path = config_path or resolve_writable_config(args.config)

    # A single shot: --once or a dry run.
    if args.once or args.dry_run:
        return 0 if check_and_report(url, token, service_id, write_path, last_ips, dry_run=args.dry_run) else 1

    # Daemon mode (default): report immediately, then every `interval` seconds.
    log(f"ip-reporter started: reporting every {interval}s to {url}")
    while True:
        check_and_report(url, token, service_id, write_path, last_ips)
        time.sleep(max(1, interval))


if __name__ == "__main__":
    sys.exit(main())
