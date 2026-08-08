[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$ContainerName = "canvas-searxng",
    [int]$Port = 8888,
    [int]$TimeoutSeconds = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 120) {
    throw "TimeoutSeconds must be between 1 and 120."
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535."
}

$stateRoot = [Environment]::GetFolderPath("LocalApplicationData")
if ([string]::IsNullOrWhiteSpace($stateRoot)) {
    $stateRoot = [IO.Path]::GetTempPath()
}
$stateDirectory = Join-Path $stateRoot "canvas-obsidian-sync"
$statePath = Join-Path $stateDirectory "local-search-keeper.json"
$healthUrl = "http://127.0.0.1:$Port/"

function Test-SearchHttp {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        return [int]$response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Get-RecordedKeeper {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $null
    }
    try {
        $record = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if (
            [int]$record.schema_version -ne 2 -or
            [string]$record.distro -ne $Distro -or
            [string]$record.container -ne $ContainerName
        ) {
            return $null
        }
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        if ($process.ProcessName -notin @("wsl", "wsl.exe")) {
            return $null
        }
        if (
            $process.StartTime.ToUniversalTime().Ticks -ne
            [long]$record.process_start_time_utc_ticks
        ) {
            return $null
        }
        return $process
    }
    catch {
        return $null
    }
}

$keeper = Get-RecordedKeeper
if ($null -eq $keeper) {
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $keeper = Start-Process -FilePath "wsl.exe" -ArgumentList @(
        "--distribution", $Distro,
        "--exec", "/usr/bin/sleep", "infinity"
    ) -WindowStyle Hidden -PassThru
    [pscustomobject]@{
        schema_version = 2
        pid = $keeper.Id
        process_start_time_utc_ticks = $keeper.StartTime.ToUniversalTime().Ticks
        distro = $Distro
        container = $ContainerName
        started_at_utc = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $statePath -Encoding UTF8
}

if (Test-SearchHttp) {
    [pscustomobject]@{
        ok = $true
        state = "already-running"
        endpoint = $healthUrl
        keeper_pid = $keeper.Id
    } | ConvertTo-Json -Compress
    exit 0
}

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$containerStartSucceeded = $false
do {
    & wsl.exe --distribution $Distro -- systemctl is-active --quiet docker 2>$null
    if ($LASTEXITCODE -eq 0 -and -not $containerStartSucceeded) {
        & wsl.exe --distribution $Distro -- docker start $ContainerName 2>$null | Out-Null
        $containerStartSucceeded = $LASTEXITCODE -eq 0
    }
    if (Test-SearchHttp) {
        [pscustomobject]@{
            ok = $true
            state = "started"
            endpoint = $healthUrl
            keeper_pid = $keeper.Id
        } | ConvertTo-Json -Compress
        exit 0
    }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $deadline)

throw "Local search did not become healthy within $TimeoutSeconds seconds. Run status.ps1 and inspect the canvas-searxng container."
