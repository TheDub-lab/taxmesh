<#
.SYNOPSIS
    End-to-end taxmesh demo: seed -> Atlas engine -> flow -> controls -> go-live.

.DESCRIPTION
    Clean run (default): every control, the parallel run, and the go-live gate
    should PASS.

    Break run (-Breaks): bad data is injected and the platform is expected to
    *catch* it. Match breaks (price variance, no PO, duplicate invoice) block
    vouchers so they never reach the GL; the AP controls stay internally
    consistent and name the exceptions; a legacy drift makes the parallel run
    report an unexpected variance; and the go-live gate returns NOT_READY.

.PARAMETER Phase
    Which domain to run: 'ar' (revenue), 'ap' (procure-to-pay), or 'all'.

.PARAMETER Breaks
    Inject faults to prove the controls and gates fail closed.

.PARAMETER Port
    Engine port to wait on (must match config.json engine.port).

.EXAMPLE
    .\run_demo.ps1
    .\run_demo.ps1 -Phase ap
    .\run_demo.ps1 -Breaks
#>
[CmdletBinding()]
param(
    [ValidateSet('ar', 'ap', 'all')]
    [string]$Phase = 'all',
    [switch]$Breaks,
    [int]$Port = 8077
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv interpreter not found at $python -- create the venv first (see README)."
}

function Invoke-Step {
    param(
        [string]$Label,
        [string[]]$ScriptArgs,
        [switch]$ExpectFail
    )
    Write-Host "`n== $Label ==" -ForegroundColor Cyan
    & $python @ScriptArgs
    $code = $LASTEXITCODE
    if ($ExpectFail) {
        if ($code -eq 0) { throw "$Label was expected to fail but exited 0" }
        Write-Host "($Label failed as expected, exit $code)" -ForegroundColor DarkYellow
    }
    elseif ($code -ne 0) {
        throw "$Label failed (exit $code)"
    }
}

$runAr = $Phase -in @('ar', 'all')
$runAp = $Phase -in @('ap', 'all')

if ($runAr) {
    $seedArgs = @('run_seed.py')
    if ($Breaks) { $seedArgs += @('--inject', 'drop_event,dlq_event') }
    Invoke-Step -Label 'Seed synthetic revenue sources' -ScriptArgs $seedArgs
}
if ($runAp) {
    $seedApArgs = @('run_seed_ap.py')
    if ($Breaks) { $seedApArgs += @('--inject', 'price_variance,no_po,duplicate_invoice') }
    # AP shares the engine transaction log and the GL journal with AR; an AP-only
    # run must clear that shared state, but 'all' already resets via run_seed.py.
    if ($Phase -eq 'ap') { $seedApArgs += @('--reset') }
    Invoke-Step -Label 'Seed synthetic procure-to-pay sources' -ScriptArgs $seedApArgs
}

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

    if ($runAr) {
        Invoke-Step -Label 'Revenue flow (billing -> engine -> GL -> mart)' -ScriptArgs @('run_revenue_flow.py')
        Invoke-Step -Label 'Revenue controls / tie-outs' -ScriptArgs @('run_controls.py') -ExpectFail:$Breaks
    }
    if ($runAp) {
        Invoke-Step -Label 'AP flow (match -> input/use tax -> GL -> subledger -> mart)' -ScriptArgs @('run_ap_flow.py')
        Invoke-Step -Label 'AP controls / tie-outs' -ScriptArgs @('run_ap_controls.py')
        Invoke-Step -Label 'Parallel run (new vs legacy AP)' -ScriptArgs @('run_parallel_run.py')
        $goLiveArgs = @('run_go_live.py')
        if ($Breaks) { $goLiveArgs += @('--inject', 'legacy_drift') }
        Invoke-Step -Label 'Go-live readiness gate' -ScriptArgs $goLiveArgs -ExpectFail:$Breaks
    }
}
finally {
    if ($engine -and -not $engine.HasExited) { Stop-Process -Id $engine.Id -Force }
    Write-Host 'engine stopped'
}

if ($Breaks) {
    Write-Host "`nBreak run complete: the platform caught the injected faults and the gate refused go-live." -ForegroundColor Yellow
} else {
    Write-Host "`nClean run complete: every control and gate should be PASS." -ForegroundColor Green
}
