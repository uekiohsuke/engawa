# 縁側を起動する：コアをバックグラウンドで起動し、準備ができたら UI を開く。UI を閉じるとコアも止める。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$port = if ($env:ENGAWA_CORE_PORT) { $env:ENGAWA_CORE_PORT } else { "8765" }
$health = "http://127.0.0.1:$port/health"

$core = Start-Process -FilePath $python -ArgumentList "-m", "engawa.core" -PassThru -WindowStyle Hidden
try {
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-RestMethod $health -TimeoutSec 5 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw "コアが起動しませんでした（$health）" }
    & $python -m engawa.ui
}
finally {
    if (-not $core.HasExited) { Stop-Process -Id $core.Id -Force }
}
