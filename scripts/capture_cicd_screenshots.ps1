# Captures the CI/CD screenshots in docs/images/cicd/ from the public GitHub
# Actions pages, with headless Microsoft Edge (no login needed: the repo is
# public). Run IDs are pinned so the "before" and "after" pictures stay the
# same runs; update them when re-capturing.
#
#   powershell -File scripts/capture_cicd_screenshots.ps1

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $repo "docs\images\cicd"
New-Item -ItemType Directory -Force $out | Out-Null
$edge = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge not found" }
$profileDir = Join-Path $env:TEMP "agentic-de-edge-headless"
$base = "https://github.com/sumanthreddy369/databricks-agentic-de"

$shots = @(
    # Before: one lint+test job, and a bundle check that only ever skipped.
    @{ File = "01_before_ci.png"; Size = "1400,1000"; Url = "$base/actions/runs/38764515721" },
    @{ File = "02_before_bundle_validate.png"; Size = "1400,900"; Url = "$base/actions/runs/38764515511" },
    # After: staged CI, the lint gate stopping a bad commit, bundle stages, workflow lint.
    @{ File = "03_after_ci.png"; Size = "1400,2300"; Url = "$base/actions/runs/38819990569" },
    @{ File = "04_after_ci_gate_caught_lint.png"; Size = "1400,1000"; Url = "$base/actions/runs/38819893751" },
    @{ File = "05_after_bundle_stages.png"; Size = "1400,1100"; Url = "$base/actions/runs/38819990583" },
    @{ File = "06_workflow_lint.png"; Size = "1400,900"; Url = "$base/actions/runs/38819261468" },
    @{ File = "07_dependabot_prs.png"; Size = "1400,1100"; Url = "$base/pulls" },
    # The run list keeps loading; a shorter time budget lets the shot complete.
    @{ File = "08_actions_history.png"; Size = "1400,1300"; Url = "$base/actions"; Budget = 8000 }
)

foreach ($shot in $shots) {
    $file = Join-Path $out $shot.File
    & $edge --headless=new --disable-gpu --hide-scrollbars --user-data-dir="$profileDir" `
        --window-size=$($shot.Size) --virtual-time-budget=$(if ($shot.Budget) { $shot.Budget } else { 15000 }) `
        --screenshot="$file" $shot.Url 2>$null
    # Edge can return before the file lands; wait up to 30 s for it.
    for ($i = 0; $i -lt 30 -and -not (Test-Path $file); $i++) { Start-Sleep -Seconds 1 }
    if (Test-Path $file) { "{0}  {1:N0} bytes" -f $shot.File, (Get-Item $file).Length } else { "{0}  MISSING" -f $shot.File }
}
