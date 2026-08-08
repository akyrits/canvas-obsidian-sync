[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$ContainerName = "canvas-searxng",
    [int]$Port = 8888
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535."
}

$stateRoot = [Environment]::GetFolderPath("LocalApplicationData")
if ([string]::IsNullOrWhiteSpace($stateRoot)) {
    $stateRoot = [IO.Path]::GetTempPath()
}
$statePath = Join-Path (Join-Path $stateRoot "canvas-obsidian-sync") "local-search-keeper.json"
$healthUrl = "http://127.0.0.1:$Port/"
$keeperAlive = $false
$keeperPid = $null

if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $record = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if (
            [int]$record.schema_version -ne 2 -or
            [string]$record.distro -ne $Distro -or
            [string]$record.container -ne $ContainerName
        ) {
            throw "Keeper state identity does not match this local-search instance."
        }
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        if (
            $process.ProcessName -in @("wsl", "wsl.exe") -and
            $process.StartTime.ToUniversalTime().Ticks -eq
            [long]$record.process_start_time_utc_ticks
        ) {
            $keeperAlive = $true
            $keeperPid = $process.Id
        }
    }
    catch {
        $keeperAlive = $false
    }
}

$httpHealthy = $false
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
    $httpHealthy = [int]$response.StatusCode -eq 200
}
catch {
    $httpHealthy = $false
}

[pscustomobject]@{
    ok = ($keeperAlive -and $httpHealthy)
    keeper_alive = $keeperAlive
    keeper_pid = $keeperPid
    http_healthy = $httpHealthy
    endpoint = $healthUrl
} | ConvertTo-Json -Compress

if (-not ($keeperAlive -and $httpHealthy)) {
    exit 1
}
