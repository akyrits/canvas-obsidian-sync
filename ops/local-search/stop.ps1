[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$ContainerName = "canvas-searxng"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$stateRoot = [Environment]::GetFolderPath("LocalApplicationData")
if ([string]::IsNullOrWhiteSpace($stateRoot)) {
    $stateRoot = [IO.Path]::GetTempPath()
}
$statePath = Join-Path (Join-Path $stateRoot "canvas-obsidian-sync") "local-search-keeper.json"

if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    [pscustomobject]@{ok = $true; state = "already-stopped"} | ConvertTo-Json -Compress
    exit 0
}

$record = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if (
    [int]$record.schema_version -ne 2 -or
    [string]$record.distro -ne $Distro -or
    [string]$record.container -ne $ContainerName
) {
    throw "Keeper state identity does not match this local-search instance; refusing to stop anything."
}

try {
    $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
    if ($process.ProcessName -notin @("wsl", "wsl.exe")) {
        throw "Recorded keeper PID belongs to another process; refusing to stop it."
    }
    if (
        $process.StartTime.ToUniversalTime().Ticks -ne
        [long]$record.process_start_time_utc_ticks
    ) {
        throw "Recorded keeper PID was reused by another process; refusing to stop it."
    }
}
catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
    # A stale state record is safe to remove; no matching process remains.
    Remove-Item -LiteralPath $statePath -Force
    [pscustomobject]@{ok = $true; state = "already-stopped"} | ConvertTo-Json -Compress
    exit 0
}

& wsl.exe --distribution $Distro -- docker stop --time 5 $ContainerName 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The named SearXNG container could not be stopped; the keeper remains running."
}

Stop-Process -Id $process.Id
Wait-Process -Id $process.Id -Timeout 10 -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $statePath -Force
[pscustomobject]@{ok = $true; state = "stopped"} | ConvertTo-Json -Compress
