# Captures full-resolution screenshots of each locally running stack into
# docs/images/stack/, with headless Microsoft Edge (Windows). Start the stacks
# first:
#   docker compose up -d                                  # Redpanda + console
#   docker compose --profile observability up -d          # Collector, Prometheus, Jaeger, Grafana
#   uv run python -m agent.telemetry ...                  # metrics (see README)
#
#   powershell -File scripts/capture_stack_screenshots.ps1
#
# The Jaeger trace (an expanded span) and MLflow (a first-run popup covers the
# page) need clicks, so they are taken by hand; this script doesn't touch them.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $repo "docs\images\stack"
$edge = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge not found" }
# A separate profile forces a fresh headless process (otherwise a running Edge takes the request).
$profileDir = Join-Path $env:TEMP "agentic-de-edge-headless"

$shots = @(
    @{ File = "grafana_dashboard.png"; Size = "1500,2060"; Url = "http://localhost:3000/d/agentic-de?orgId=1&kiosk" },
    @{ File = "prometheus_query.png"; Size = "1400,520"; Url = "http://localhost:9090/graph?g0.expr=max%20by%20(event)%20(agentic_de_safety_events)%20or%20max%20by%20(reason)%20(agentic_de_requests_needing_person)%20or%20max%20by%20(outcome)%20(agentic_de_approvals)&g0.tab=1" },
    @{ File = "redpanda_topic.png"; Size = "1400,820"; Url = "http://localhost:8080/topics/patient_events" }
)

foreach ($shot in $shots) {
    $target = Join-Path $out $shot.File
    $args = @(
        "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--user-data-dir=$profileDir", "--window-size=$($shot.Size)",
        "--virtual-time-budget=20000", "--screenshot=$target", $shot.Url
    )
    $p = Start-Process -FilePath $edge -ArgumentList $args -PassThru -Wait
    if ($p.ExitCode -ne 0 -or -not (Test-Path $target)) { Write-Warning "failed: $($shot.File)" }
    else { "wrote $target" }
}
