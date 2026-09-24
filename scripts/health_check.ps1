param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$MetricsToken = $env:METRICS_TOKEN
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")

function Assert-HttpOk {
    param(
        [string]$Name,
        [string]$Url,
        [hashtable]$Headers = @{},
        [int[]]$Expected = @(200)
    )
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method Get -Uri $Url -Headers $Headers
        if ($Expected -notcontains [int]$response.StatusCode) {
            Write-Host "FAILED: $Name expected $($Expected -join ','), got $($response.StatusCode)" -ForegroundColor Red
            Write-Host $response.Content
            exit 1
        }
        Write-Host "OK: $Name -> $($response.StatusCode)" -ForegroundColor Green
        return $response
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) {
            Write-Host "FAILED: $Name request failed: $($_.Exception.Message)" -ForegroundColor Red
            exit 1
        }
        $status = [int]$response.StatusCode
        if ($Expected -contains $status) {
            Write-Host "OK: $Name -> $status" -ForegroundColor Green
            return $response
        }
        Write-Host "FAILED: $Name expected $($Expected -join ','), got $status" -ForegroundColor Red
        exit 1
    }
}

$health = Assert-HttpOk -Name "liveness /health" -Url "$BaseUrl/health"
$ready = Assert-HttpOk -Name "readiness /ready" -Url "$BaseUrl/ready"

$headers = @{}
if (-not [string]::IsNullOrWhiteSpace($MetricsToken)) {
    $headers["X-Metrics-Token"] = $MetricsToken
}
$metrics = Assert-HttpOk -Name "metrics /metrics" -Url "$BaseUrl/metrics" -Headers $headers
if ($metrics.Content -notmatch "processing_platform_http_requests_total") {
    Write-Host "FAILED: metrics payload does not include HTTP counters" -ForegroundColor Red
    exit 1
}
Write-Host "OK: metrics payload includes HTTP counters" -ForegroundColor Green

Write-Host "Observability health check passed." -ForegroundColor Green
