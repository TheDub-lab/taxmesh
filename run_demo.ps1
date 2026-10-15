<#
.SYNOPSIS
    End-to-end taxmesh demo: seed -> start Atlas engine -> revenue flow -> controls.

.DESCRIPTION
    Clean run (default): every control should PASS.
    Break run (-Breaks): a dropped billing event and an unmapped ship-to postal
    code are injected; the flow still reports COMPLETED, the dead letter queue
    catches the poison event, and the controls FAIL while naming each exception.

.EXAMPLE
    .\run_demo.ps1
    .\run_demo.ps1 -Breaks
#>
[CmdletBinding()]
param(
    [switch]$Breaks,
    [int]$Port = 8077
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv interpreter not found at $python -- create the venv first (see README)."
}

function Invoke-Step {
    param([string]$Label, [string[]]$ScriptArgs)
    Write-Host "`n== $Label ==" -ForegroundColor Cyan
    & $python @ScriptArgs
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

$seedArgs = @('run_seed.py')
if ($Breaks) { $seedArgs += @('--inject', 'drop_event,dlq_event') }

Invoke-Step -Label 'Seed synthetic sources' -ScriptArgs $seedArgs

Write-Host "`n== Start Atlas tax engine ==" -ForegroundColor Cyan
$engine = Start-Process -FilePath $python -ArgumentList 'run_engine.py' -PassThru -WindowStyle Hidden
try {
    $up = $false
    for ($i = 0; $i -lt 80; $i++) {
        try {
            Invoke-RestMethod "http://127.0.0.1:$Port/v2/health" -TimeoutSec 2 | Out-Null
            $up = $true; break
        } catch { Start-Sleep -Milliseconds 250 }
    }
    if (-not $up) { throw "engine did not become healthy on port $Port" }
    Write-Host "engine healthy on http://127.0.0.1:$Port"

    Invoke-Step -Label 'Revenue flow (billing -> engine -> GL -> mart)' -ScriptArgs @('run_revenue_flow.py')
    Invoke-Step -Label 'Controls / tie-outs' -ScriptArgs @('run_controls.py')
}
finally {
    if ($engine -and -not $engine.HasExited) { Stop-Process -Id $engine.Id -Force }
    Write-Host 'engine stopped'
}

if ($Breaks) {
    Write-Host "`nBreak run complete: controls were expected to FAIL and name the exceptions." -ForegroundColor Yellow
} else {
    Write-Host "`nClean run complete: every control should be PASS." -ForegroundColor Green
}
