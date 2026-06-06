class IpReporter < Formula
  desc "Report a machine's WAN and LAN IP to the telemetry gateway on a schedule"
  homepage "https://github.com/guocity/homebrew-ip-reporter"
  url "https://github.com/guocity/homebrew-ip-reporter.git", tag: "v2.0.1", revision: "07d87d688f365c3c9b748c8e1c8a0feb44abe96f"
  license "MIT"
  head "https://github.com/guocity/homebrew-ip-reporter.git", branch: "main"

  depends_on "rust" => :build

  def install
    # Build the native Rust binary and install it to bin/.
    system "cargo", "install", *std_cargo_args

    # Seed a config file under etc (Homebrew preserves your edits across upgrades).
    (etc/"ip-reporter").install "config.example.env" => "config.env"
  end

  service do
    run [opt_bin/"ip-reporter"]
    keep_alive true
    run_at_load true
    log_path var/"log/ip-reporter.log"
    error_log_path var/"log/ip-reporter.log"
    environment_variables IP_REPORTER_CONFIG: etc/"ip-reporter/config.env"
  end

  def caveats
    <<~EOS
      Set the token and start the hourly service in one step (cloudflared-style):

        ip-reporter install --token <TOKEN>

      Mint a token on the telemetry server host, bound to this hostname:
        node server/manage-tokens.js generate "$(hostname)" telemetry

      Other cadence?  ip-reporter install --token <TOKEN> --interval 1800
      Already configured?  brew services start ip-reporter
      Run directly (no service):  ip-reporter --token <TOKEN>
      Dry run:  ip-reporter --print

      Config: #{etc}/ip-reporter/config.env
      Logs:   #{var}/log/ip-reporter.log
    EOS
  end

  test do
    assert_match "WAN/LAN IP", shell_output("#{bin}/ip-reporter --help")
    # --print collects and prints the payload without sending or needing a token.
    assert_match "Payload:", shell_output("#{bin}/ip-reporter --print 2>&1")
  end
end
