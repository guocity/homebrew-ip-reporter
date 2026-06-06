//! ip-reporter — report this machine's WAN and LAN IP to the telemetry gateway.
//!
//! A tiny native agent (Rust port of the original `ip_reporter.py`). Each run, or
//! each interval tick, it:
//!   1. Reads the machine hostname.
//!   2. Discovers the primary LAN IPv4 address (UDP-connect trick, no packets sent).
//!   3. Looks up the public WAN IP via the first external provider that responds.
//!   4. POSTs a small JSON payload to the gateway with a Bearer token.
//!
//! Settings resolve with the precedence:  CLI flag > environment variable >
//! config file > built-in default. The optional config file is a KEY=VALUE file;
//! recognized keys are TELEMETRY_TOKEN, TELEMETRY_URL, SERVICE_ID, REPORT_INTERVAL.
//!
//! Footprint-first: blocking, single-threaded, sleeps between reports. No async
//! runtime; bundled rustls TLS so there is no system OpenSSL dependency.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::net::UdpSocket;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const DEFAULT_TELEMETRY_URL: &str = "https://telemetry.lgnat.com/api/telemetry";
const DEFAULT_INTERVAL: u64 = 3600; // seconds (1 hour)
const HTTP_TIMEOUT: Duration = Duration::from_secs(10);
const USER_AGENT: &str = "ip-reporter/2.0";
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Services that echo back the caller's public IP as plain text.
/// The Cloudflare trace endpoint is parsed specially (`ip=...`).
const WAN_IP_PROVIDERS: &[&str] = &[
    "https://cloudflare.com/cdn-cgi/trace",
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
];

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

/// Timestamped line to stderr (service managers capture this to a log file).
fn log(msg: &str) {
    let stamp = fmt_timestamp(now_secs());
    let mut err = std::io::stderr().lock();
    let _ = writeln!(err, "[{stamp}] {msg}");
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Format a Unix timestamp as `YYYY-MM-DD HH:MM:SS UTC` (no chrono dependency).
fn fmt_timestamp(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (h, m, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);

    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if month <= 2 { y + 1 } else { y };

    format!("{year:04}-{month:02}-{d:02} {h:02}:{m:02}:{s:02} UTC")
}

// ---------------------------------------------------------------------------
// Config file handling
// ---------------------------------------------------------------------------

/// Config file search order (first existing wins) when --config is not given.
fn default_config_paths() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Ok(p) = std::env::var("IP_REPORTER_CONFIG") {
        if !p.is_empty() {
            paths.push(PathBuf::from(p));
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        paths.push(PathBuf::from(format!("{home}/.config/ip-reporter/config.env")));
    }
    paths.push(PathBuf::from("/opt/homebrew/etc/ip-reporter/config.env"));
    paths.push(PathBuf::from("/usr/local/etc/ip-reporter/config.env"));
    paths
}

/// Parse an optional KEY=VALUE config file. Returns an empty map if none found.
fn read_config_file(explicit: Option<&str>) -> BTreeMap<String, String> {
    let paths: Vec<PathBuf> = match explicit {
        Some(p) => vec![PathBuf::from(p)],
        None => default_config_paths(),
    };
    let mut cfg = BTreeMap::new();
    for path in paths {
        if path.is_file() {
            if let Ok(contents) = fs::read_to_string(&path) {
                for raw in contents.lines() {
                    let line = raw.trim();
                    if line.is_empty() || line.starts_with('#') || !line.contains('=') {
                        continue;
                    }
                    let (key, value) = line.split_once('=').unwrap();
                    let value = value.trim().trim_matches('"').trim_matches('\'');
                    cfg.insert(key.trim().to_string(), value.to_string());
                }
                log(&format!("Loaded config from {}", path.display()));
            }
            break;
        }
    }
    cfg
}

/// Resolve one setting: CLI flag > env var > config file > default.
fn resolve(
    cli: Option<&str>,
    env_key: &str,
    cfg: &BTreeMap<String, String>,
    cfg_key: &str,
    default: Option<&str>,
) -> Option<String> {
    if let Some(v) = cli {
        if !v.is_empty() {
            return Some(v.to_string());
        }
    }
    if let Ok(v) = std::env::var(env_key) {
        if !v.is_empty() {
            return Some(v);
        }
    }
    if let Some(v) = cfg.get(cfg_key) {
        if !v.is_empty() {
            return Some(v.clone());
        }
    }
    default.map(|s| s.to_string())
}

// ---------------------------------------------------------------------------
// Data collection
// ---------------------------------------------------------------------------

// Kept for when the hostname field is re-enabled in build_payload().
#[allow(dead_code)]
fn get_hostname() -> String {
    gethostname::gethostname().to_string_lossy().into_owned()
}

/// Primary outbound LAN IPv4 address. Opens a UDP socket toward a public address
/// and inspects the local end of the route — no packets are actually sent.
fn get_lan_ip() -> Option<String> {
    let sock = UdpSocket::bind("0.0.0.0:0").ok()?;
    sock.connect("8.8.8.8:80").ok()?;
    sock.local_addr().ok().map(|a| a.ip().to_string())
}

/// Public WAN IP via the first provider that responds.
fn get_wan_ip(agent: &ureq::Agent) -> Option<String> {
    for &url in WAN_IP_PROVIDERS {
        match agent.get(url).set("User-Agent", USER_AGENT).call() {
            Ok(resp) => {
                let body = match resp.into_string() {
                    Ok(b) => b,
                    Err(e) => {
                        log(&format!("WAN lookup via {url} failed: {e}"));
                        continue;
                    }
                };
                let body = body.trim();
                if url.contains("cdn-cgi/trace") {
                    if let Some(ip) = body
                        .lines()
                        .find_map(|l| l.strip_prefix("ip="))
                        .map(|s| s.trim().to_string())
                    {
                        return Some(ip);
                    }
                    continue;
                }
                // Plain-text providers return just the IP.
                if let Some(ip) = body.split_whitespace().next() {
                    if !ip.is_empty() {
                        return Some(ip.to_string());
                    }
                }
            }
            Err(e) => {
                log(&format!("WAN lookup via {url} failed: {e}"));
            }
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Payload + send
// ---------------------------------------------------------------------------

/// Escape a string for embedding in a JSON string literal (minimal, sufficient
/// for hostnames and IPs).
fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// One JSON field: `"key":"value"` or `"key":null`.
fn json_field(key: &str, value: &Option<String>) -> String {
    match value {
        Some(v) => format!("\"{}\":\"{}\"", json_escape(key), json_escape(v)),
        None => format!("\"{}\":null", json_escape(key)),
    }
}

/// Build the `{hostname, wan_ip, lan_ip[, service_id]}` payload as a JSON string.
fn build_payload(agent: &ureq::Agent, service_id: &Option<String>) -> String {
    let mut fields = vec![
        // service_id (derived from the token) is this machine's hostname, so the
        // hostname field is redundant — commented out for now.
        // json_field("hostname", &Some(get_hostname())),
        json_field("wan_ip", &get_wan_ip(agent)),
        json_field("lan_ip", &get_lan_ip()),
    ];
    if let Some(sid) = service_id {
        if !sid.is_empty() {
            fields.push(json_field("service_id", &Some(sid.clone())));
        }
    }
    format!("{{{}}}", fields.join(","))
}

/// POST the payload to the telemetry gateway with a Bearer token.
fn send(agent: &ureq::Agent, url: &str, token: &Option<String>, payload: &str) -> bool {
    let token = match token {
        Some(t) if !t.is_empty() => t,
        _ => {
            log("ERROR: no token. Pass --token, set TELEMETRY_TOKEN, or add it to a config file.");
            return false;
        }
    };

    let result = agent
        .post(url)
        .set("Content-Type", "application/json")
        .set("Authorization", &format!("Bearer {token}"))
        .set("User-Agent", USER_AGENT)
        .send_string(payload);

    match result {
        Ok(resp) => {
            let status = resp.status();
            let body = resp.into_string().unwrap_or_default();
            log(&format!("Sent OK ({status}): {}", body.trim()));
            true
        }
        Err(ureq::Error::Status(code, resp)) => {
            let detail = resp.into_string().unwrap_or_default();
            log(&format!(
                "ERROR: gateway returned HTTP {code}: {}",
                detail.trim()
            ));
            false
        }
        Err(e) => {
            log(&format!("ERROR: could not reach gateway {url}: {e}"));
            false
        }
    }
}

fn report_once(
    agent: &ureq::Agent,
    url: &str,
    token: &Option<String>,
    service_id: &Option<String>,
    dry_run: bool,
) -> bool {
    let payload = build_payload(agent, service_id);
    log(&format!("Payload: {payload}"));
    if dry_run {
        return true;
    }
    send(agent, url, token, &payload)
}

fn build_agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(HTTP_TIMEOUT)
        .user_agent(USER_AGENT)
        .build()
}

// ---------------------------------------------------------------------------
// `install` subcommand: bake the token into config + (re)start the service
// ---------------------------------------------------------------------------

/// Pick the config file the service reads, creating its path if needed.
fn resolve_writable_config(explicit: Option<&str>) -> PathBuf {
    if let Some(p) = explicit {
        return PathBuf::from(p);
    }
    if let Ok(p) = std::env::var("IP_REPORTER_CONFIG") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }

    let home = std::env::var("HOME").unwrap_or_default();
    let candidates = [
        PathBuf::from("/opt/homebrew/etc/ip-reporter/config.env"),
        PathBuf::from("/usr/local/etc/ip-reporter/config.env"),
        PathBuf::from(format!("{home}/.config/ip-reporter/config.env")),
    ];
    for path in &candidates {
        if path.is_file() {
            return path.clone();
        }
    }

    if let Some(prefix) = brew_prefix() {
        return PathBuf::from(prefix).join("etc/ip-reporter/config.env");
    }

    PathBuf::from(format!("{home}/.config/ip-reporter/config.env"))
}

fn brew_prefix() -> Option<String> {
    let out = Command::new("brew").arg("--prefix").output().ok()?;
    if !out.status.success() {
        return None;
    }
    let prefix = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if prefix.is_empty() {
        None
    } else {
        Some(prefix)
    }
}

/// Upsert KEY=VALUE pairs into a config file, preserving other lines. chmod 600.
fn write_config_values(path: &Path, values: &[(String, String)]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }

    let existing = fs::read_to_string(path).unwrap_or_default();
    let mut remaining: BTreeMap<&str, &str> =
        values.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();

    let mut out: Vec<String> = Vec::new();
    for line in existing.lines() {
        let stripped = line.trim();
        let key = if stripped.contains('=') {
            Some(stripped.split('=').next().unwrap().trim())
        } else {
            None
        };
        match key {
            Some(k) if remaining.contains_key(k) && !stripped.starts_with('#') => {
                let v = remaining.remove(k).unwrap();
                out.push(format!("{k}={v}"));
            }
            _ => out.push(line.to_string()),
        }
    }
    // Append any keys not already present (preserve insertion order of `values`).
    for (k, v) in values {
        if remaining.contains_key(k.as_str()) {
            out.push(format!("{k}={v}"));
        }
    }

    fs::write(path, out.join("\n") + "\n")?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

fn do_install(args: &[String]) -> i32 {
    let mut token: Option<String> = None;
    let mut token_pos: Option<String> = None;
    let mut url: Option<String> = None;
    let mut interval: Option<String> = None;
    let mut config: Option<String> = None;
    let mut no_start = false;

    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        match a.as_str() {
            "--token" => {
                token = next_value(args, &mut i, "--token");
            }
            "--url" => {
                url = next_value(args, &mut i, "--url");
            }
            "--interval" => {
                interval = next_value(args, &mut i, "--interval");
            }
            "--config" => {
                config = next_value(args, &mut i, "--config");
            }
            "--no-start" => no_start = true,
            "-h" | "--help" => {
                print_install_help();
                return 0;
            }
            s if s.starts_with("--") => {
                eprintln!("install: unknown option: {s}");
                return 2;
            }
            _ => {
                if token_pos.is_none() {
                    token_pos = Some(a.clone());
                }
            }
        }
        i += 1;
    }

    let token = token.or(token_pos);
    let token = match token {
        Some(t) if !t.is_empty() => t,
        _ => {
            eprintln!("install: a token is required (positional TOKEN or --token)");
            return 2;
        }
    };

    let target = resolve_writable_config(config.as_deref());
    let mut values = vec![("TELEMETRY_TOKEN".to_string(), token)];
    if let Some(u) = url {
        values.push(("TELEMETRY_URL".to_string(), u));
    }
    if let Some(iv) = interval {
        values.push(("REPORT_INTERVAL".to_string(), iv));
    }

    if let Err(e) = write_config_values(&target, &values) {
        log(&format!("ERROR: could not write config {}: {e}", target.display()));
        return 1;
    }
    log(&format!("Saved token to {}", target.display()));

    if no_start {
        log("Config written. Start it with: brew services start ip-reporter");
        return 0;
    }

    if which("brew").is_none() {
        log("Homebrew not found. Config written; start the service however you manage it (e.g. run: ip-reporter).");
        return 0;
    }

    log("Starting service: brew services restart ip-reporter");
    let status = Command::new("brew")
        .args(["services", "restart", "ip-reporter"])
        .status();
    match status {
        Ok(s) if s.success() => {
            log("Service running. Logs: $(brew --prefix)/var/log/ip-reporter.log");
            0
        }
        Ok(s) => s.code().unwrap_or(1),
        Err(e) => {
            log(&format!("ERROR: failed to run brew services: {e}"));
            1
        }
    }
}

/// Minimal `which`: is the executable on PATH?
fn which(cmd: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(cmd);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Argument parsing helpers + main
// ---------------------------------------------------------------------------

/// Consume the value following a flag at position `*i`, advancing `*i` past it.
fn next_value(args: &[String], i: &mut usize, flag: &str) -> Option<String> {
    if *i + 1 < args.len() {
        *i += 1;
        Some(args[*i].clone())
    } else {
        eprintln!("{flag} requires a value");
        None
    }
}

fn print_help() {
    println!(
        "Report this machine's WAN/LAN IP to the telemetry gateway.\n\
\n\
Usage: ip-reporter [OPTIONS]\n\
       ip-reporter install [TOKEN] [--token TOKEN] [--url URL] [--interval N] [--config PATH] [--no-start]\n\
\n\
Options:\n\
  --token TOKEN       Bearer token (overrides env/config).\n\
  --url URL           Gateway URL (default: {DEFAULT_TELEMETRY_URL}).\n\
  --service-id ID     Optional explicit service_id field in the payload.\n\
  --interval SECONDS  Seconds between reports (default: {DEFAULT_INTERVAL}). Ignored with --once.\n\
  --once              Report a single time and exit (e.g. under cron).\n\
  --print             Collect and print the payload without sending it (implies --once).\n\
  --config PATH       Explicit path to a KEY=VALUE config file.\n\
  -h, --help          Show this help.\n\
  -v, --version       Print the version and exit.\n\
\n\
Subcommand: `ip-reporter install --token <TOKEN>` saves the token and starts the\n\
hourly service (cloudflared-style)."
    );
}

fn print_install_help() {
    println!(
        "Save the token into the service config and (re)start the hourly service\n\
— like `cloudflared service install <TOKEN>`.\n\
\n\
Usage: ip-reporter install [TOKEN] [OPTIONS]\n\
\n\
Options:\n\
  --token TOKEN       Bearer token (may also be given positionally).\n\
  --url URL           Override the gateway URL.\n\
  --interval SECONDS  Report cadence in seconds.\n\
  --config PATH       Explicit config file path to write.\n\
  --no-start          Write the config but do not start the service."
    );
}

fn main() {
    std::process::exit(run());
}

fn run() -> i32 {
    let argv: Vec<String> = std::env::args().skip(1).collect();

    // Subcommand: `ip-reporter install [TOKEN] [--token ...]`
    if argv.first().map(|s| s.as_str()) == Some("install") {
        return do_install(&argv[1..]);
    }

    let mut cli_token: Option<String> = None;
    let mut cli_url: Option<String> = None;
    let mut cli_service_id: Option<String> = None;
    let mut cli_interval: Option<String> = None;
    let mut cli_config: Option<String> = None;
    let mut once = false;
    let mut dry_run = false;

    let mut i = 0;
    while i < argv.len() {
        match argv[i].as_str() {
            "--token" => cli_token = next_value(&argv, &mut i, "--token"),
            "--url" => cli_url = next_value(&argv, &mut i, "--url"),
            "--service-id" => cli_service_id = next_value(&argv, &mut i, "--service-id"),
            "--interval" => cli_interval = next_value(&argv, &mut i, "--interval"),
            "--config" => cli_config = next_value(&argv, &mut i, "--config"),
            "--once" => once = true,
            "--print" => dry_run = true,
            "-h" | "--help" => {
                print_help();
                return 0;
            }
            "-v" | "--version" => {
                println!("ip-reporter {VERSION} (rust)");
                return 0;
            }
            s => {
                eprintln!("unknown option: {s}");
                return 2;
            }
        }
        i += 1;
    }

    let cfg = read_config_file(cli_config.as_deref());

    let token = resolve(cli_token.as_deref(), "TELEMETRY_TOKEN", &cfg, "TELEMETRY_TOKEN", None);
    let url = resolve(
        cli_url.as_deref(),
        "TELEMETRY_URL",
        &cfg,
        "TELEMETRY_URL",
        Some(DEFAULT_TELEMETRY_URL),
    )
    .unwrap();
    let service_id = resolve(cli_service_id.as_deref(), "SERVICE_ID", &cfg, "SERVICE_ID", None);
    let interval: u64 = resolve(
        cli_interval.as_deref(),
        "REPORT_INTERVAL",
        &cfg,
        "REPORT_INTERVAL",
        None,
    )
    .and_then(|s| s.parse().ok())
    .unwrap_or(DEFAULT_INTERVAL);

    let agent = build_agent();

    // A single shot: --once or a dry run.
    if once || dry_run {
        return if report_once(&agent, &url, &token, &service_id, dry_run) {
            0
        } else {
            1
        };
    }

    // Daemon mode (default): report immediately, then every `interval` seconds.
    log(&format!(
        "ip-reporter started: reporting every {interval}s to {url}"
    ));
    loop {
        report_once(&agent, &url, &token, &service_id, false);
        std::thread::sleep(Duration::from_secs(interval.max(1)));
    }
}
