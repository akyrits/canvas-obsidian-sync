param(
    [Parameter(Mandatory = $true)]
    [string]$EnvPath
)

$ErrorActionPreference = "Stop"
$resolvedEnv = (Resolve-Path -LiteralPath $EnvPath).Path
$updates = [ordered]@{
    RESEARCH_PROVIDER = "searxng"
    SEARXNG_BASE_URL = "http://127.0.0.1:8888"
    RESEARCH_CACHE_PATH = "C:/Users/kyrit/AppData/Local/canvas-obsidian-sync/research-cache"
    RESEARCH_COST_PER_REQUEST_USD = "0"
}

$lines = [IO.File]::ReadAllLines($resolvedEnv)
$written = @{}
$output = [Collections.Generic.List[string]]::new()

foreach ($line in $lines) {
    $match = [regex]::Match($line, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
    if ($match.Success -and $updates.Contains($match.Groups[1].Value)) {
        $key = $match.Groups[1].Value
        if (-not $written.ContainsKey($key)) {
            $output.Add("$key=$($updates[$key])")
            $written[$key] = $true
        }
        continue
    }
    $output.Add($line)
}

foreach ($key in $updates.Keys) {
    if (-not $written.ContainsKey($key)) {
        $output.Add("$key=$($updates[$key])")
    }
}

$temporary = "$resolvedEnv.local-search.tmp"
$backup = "$resolvedEnv.local-search.backup"
$encoding = [Text.UTF8Encoding]::new($false)
try {
    [IO.File]::WriteAllLines($temporary, $output, $encoding)
    [IO.File]::Replace($temporary, $resolvedEnv, $backup)
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Force
    }
}

Write-Output "Updated only the four local-search keys in .env."
