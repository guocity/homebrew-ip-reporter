class IpReporter < Formula
  desc "Report a machine's WAN and LAN IP to the telemetry gateway on a schedule"
  homepage "https://github.com/guocity/homebrew-ip-reporter"
  url "https://github.com/guocity/homebrew-ip-reporter.git", tag: "v1.0.0", revision: "21f729abdd7c4709c9398c8820ca274eb6585f2f"
  license "MIT"
  head "https://github.com/guocity/homebrew-ip-reporter.git", branch: "main"

  depends_on "python@3.13"

  def install
    libexec.install "ip_reporter.py"

    # Thin wrapper so `ip-reporter` runs the script with the formula's Python.
    (bin/"ip-reporter").write <<~SH
      #!/bin/bash
      exec "#{Formula["python@3.13"].opt_bin}/python3.13" "#{libexec}/ip_reporter.py" "$@"
    SH
    chmod 0755, bin/"ip-reporter"

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
      Before starting the service, set this machine's telemetry token:

        1) On the telemetry server host, mint a token bound to this hostname:
             node server/manage-tokens.js generate "$(hostname)" telemetry
        2) Put it in the config file (also set REPORT_INTERVAL if you want a
           cadence other than 3600s):
             #{etc}/ip-reporter/config.env
        3) Start (and enable at login) the hourly reporter:
             brew services start ip-reporter

      Or skip the config entirely and run it directly:
             ip-reporter --token <TOKEN> --interval 3600
             ip-reporter --print          # dry run, shows the payload

      Logs: #{var}/log/ip-reporter.log
    EOS
  end

  test do
    assert_match "WAN/LAN IP", shell_output("#{bin}/ip-reporter --help")
    # --print collects and prints the payload without sending or needing a token.
    assert_match "Payload:", shell_output("#{bin}/ip-reporter --print 2>&1")
  end
end
