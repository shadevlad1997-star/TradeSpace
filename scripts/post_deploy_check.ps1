param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,
    [string]$ApiKey = "",
    [string]$SecretKey = "",
    [switch]$SkipMerchant
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")

function Assert-HttpStatus {
    param(
        [string]$Name,
        [string]$Url,
        [int[]]$Expected
    )
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url
        $status = [int]$response.StatusCode
    } catch {
        if ($null -eq $_.Exception.Response) {
            Write-Host "FAILED: $Name request failed: $($_.Exception.Message)" -ForegroundColor Red
            exit 1
        }
        $status = [int]$_.Exception.Response.StatusCode
    }
    if ($Expected -notcontains $status) {
        Write-Host "FAILED: $Name expected $($Expected -join ','), got $status" -ForegroundColor Red
        exit 1
    }
    Write-Host "OK: $Name -> $status" -ForegroundColor Green
}

Assert-HttpStatus -Name "health" -Url "$BaseUrl/health" -Expected @(200)
Assert-HttpStatus -Name "ready" -Url "$BaseUrl/ready" -Expected @(200)
Assert-HttpStatus -Name "docs closed" -Url "$BaseUrl/docs" -Expected @(404)
Assert-HttpStatus -Name "openapi closed" -Url "$BaseUrl/openapi.json" -Expected @(404)
Assert-HttpStatus -Name "metrics closed" -Url "$BaseUrl/metrics" -Expected @(404, 403)

& powershell -ExecutionPolicy Bypass -File .\scripts\security_acceptance.ps1 -BaseUrl $BaseUrl -ExpectProductionProxy

if (-not $SkipMerchant) {
    if ([string]::IsNullOrWhiteSpace($ApiKey) -or [string]::IsNullOrWhiteSpace($SecretKey)) {
        Write-Host "WARNING: merchant acceptance skipped because -ApiKey and -SecretKey were not provided" -ForegroundColor Yellow
    } else {
        & powershell -ExecutionPolicy Bypass -File .\scripts\merchant_acceptance.ps1 -BaseUrl $BaseUrl -ApiKey $ApiKey -SecretKey $SecretKey
    }
}

Write-Host "Post-deploy checks completed." -ForegroundColor Green
