[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$Query = "Python dataclasses official documentation"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$agent = Join-Path $ProjectRoot "agent.py"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python was not found at $python"
}

$output = & $python $agent research $Query --max-results 3 --refresh 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Project research canary failed: $($output -join ' ')"
}

$payload = ($output -join "`n") | ConvertFrom-Json
if ($payload.usage.provider -ne "searxng") {
    throw "Expected provider=searxng."
}
if ($payload.usage.source -ne "live" -or $payload.usage.provider_requests -ne 1) {
    throw "Expected one live provider request."
}
if ($payload.usage.estimated_cost_usd -ne 0) {
    throw "Expected zero configured provider cost."
}
if ($payload.usage.model_attempted -or $payload.usage.input_tokens -ne 0 -or $payload.usage.output_tokens -ne 0) {
    throw "The model/token-free research invariant was violated."
}
if ($payload.result_count -lt 1 -or $payload.result_count -gt 3) {
    throw "Expected between one and three normalized results."
}
foreach ($hit in $payload.hits) {
    $uri = [Uri]$hit.url
    if ($uri.Scheme -ne "https") {
        throw "A normalized result did not use HTTPS."
    }
}

[pscustomobject]@{
    ok = $true
    provider = $payload.usage.provider
    source = $payload.usage.source
    provider_requests = $payload.usage.provider_requests
    estimated_cost_usd = $payload.usage.estimated_cost_usd
    model_attempted = $payload.usage.model_attempted
    input_tokens = $payload.usage.input_tokens
    output_tokens = $payload.usage.output_tokens
    result_count = $payload.result_count
    request_sha256 = $payload.request_sha256
} | ConvertTo-Json -Compress
