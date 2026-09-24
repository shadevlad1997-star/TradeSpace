param(
    [string]$OutputPath = ".env.production.generated",
    [string]$Domain = "pay.example.com",
    [string]$PostgresUser = "processor",
    [string]$PostgresDb = "processor_db"
)

$ErrorActionPreference = "Stop"

function New-RandomBytes {
    param([int]$Length)
    $bytes = New-Object byte[] $Length
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return $bytes
}

function New-UrlSafeToken {
    param([int]$Bytes = 48)
    $raw = [Convert]::ToBase64String((New-RandomBytes -Length $Bytes))
    return $raw.TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function New-FernetKey {
    $raw = [Convert]::ToBase64String((New-RandomBytes -Length 32))
    return $raw.Replace("+", "-").Replace("/", "_")
}

function New-Base32Secret {
    param([int]$Length = 32)
    $alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    $bytes = New-RandomBytes -Length $Length
    $chars = New-Object System.Text.StringBuilder
    foreach ($byte in $bytes) {
        [void]$chars.Append($alphabet[$byte % $alphabet.Length])
    }
    return $chars.ToString()
}

function New-StrongPassword {
    return (New-UrlSafeToken -Bytes 36)
}

$domain = $Domain.Trim().TrimEnd("/")
if ([string]::IsNullOrWhiteSpace($domain) -or $domain -eq "pay.example.com") {
    Write-Host "WARNING: Domain is still pay.example.com. Replace it before production." -ForegroundColor Yellow
}

$postgresPassword = New-StrongPassword
$redisPassword = New-StrongPassword
$secretKey = New-UrlSafeToken -Bytes 64
$encryptionKey = New-FernetKey
$superadminPassword = New-StrongPassword
$superadminTotp = New-Base32Secret -Length 32
$metricsToken = New-UrlSafeToken -Bytes 48
$grafanaPassword = New-StrongPassword

$content = @"
APP_NAME=Processing Platform
ENV=production
DEBUG=false
SECRET_KEY=$secretKey
JWT_ACCESS_MINUTES=20
JWT_REFRESH_DAYS=7
POSTGRES_USER=$PostgresUser
POSTGRES_PASSWORD=$postgresPassword
POSTGRES_DB=$PostgresDb
DATABASE_URL=postgresql+asyncpg://$PostgresUser`:$postgresPassword@db:5432/$PostgresDb
SYNC_DATABASE_URL=postgresql+psycopg://$PostgresUser`:$postgresPassword@db:5432/$PostgresDb
REDIS_PASSWORD=$redisPassword
REDIS_URL=redis://:$redisPassword@redis:6379/0
CELERY_BROKER_URL=redis://:$redisPassword@redis:6379/1
CELERY_RESULT_BACKEND=redis://:$redisPassword@redis:6379/2
CORS_ORIGINS=https://$domain
CSRF_TRUSTED_ORIGINS=https://$domain
TRUSTED_HOSTS=$domain,api
TRUSTED_PROXY_IPS=127.0.0.1,::1,172.16.0.0/12
ENCRYPTION_KEY=$encryptionKey
SUPERADMIN_EMAIL=superadmin@$domain
SUPERADMIN_PASSWORD=$superadminPassword
SUPERADMIN_2FA_SECRET=$superadminTotp
SEED_DEMO_DATA=false
PLATFORM_WEBHOOK_RETRY_LIMIT=5
RATE_LIMIT_DEFAULT=120/minute
LOGIN_RATE_LIMIT=10/minute
MERCHANT_RATE_LIMIT=300/minute
RATE_LIMIT_FAIL_CLOSED_IN_PRODUCTION=true
RATE_LIMIT_REDIS_FAILURE_RETRY_AFTER_SECONDS=30
HMAC_TIMESTAMP_TOLERANCE_SECONDS=300
SESSION_COOKIE_NAME=processing_session
MAX_REQUEST_BODY_BYTES=6000000
DEFAULT_MIN_AMOUNT=1
DEFAULT_MAX_AMOUNT=1000000
DOCS_ENABLED=false
OPENAPI_ENABLED=false
HSTS_ENABLED=true
HSTS_MAX_AGE_SECONDS=31536000
LOG_LEVEL=INFO
LOG_FORMAT=json
REQUEST_ID_HEADER=X-Request-ID
METRICS_ENABLED=true
METRICS_TOKEN=$metricsToken
GRAFANA_ADMIN_PASSWORD=$grafanaPassword
NGINX_HTTP_PORT=80
NGINX_HTTPS_PORT=443
SETTLEMENT_USDT_RUB_RATE=100.00
SETTLEMENT_TRANSFER_FEE_USDT=5.00
RAPIRA_RATES_ENABLED=true
RAPIRA_RATES_URL=https://api.rapira.net/open/market/rates
RAPIRA_RATES_TIMEOUT_SECONDS=5
RAPIRA_RATES_CACHE_SECONDS=15
"@

$resolvedOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
Set-Content -LiteralPath $resolvedOutput -Value $content -Encoding UTF8
Write-Host "Production env generated: $resolvedOutput" -ForegroundColor Green
Write-Host "Superadmin email: superadmin@$domain" -ForegroundColor Cyan
Write-Host "Superadmin password: $superadminPassword" -ForegroundColor Cyan
Write-Host "Superadmin TOTP secret: $superadminTotp" -ForegroundColor Cyan
Write-Host "Store these values in a password manager before moving the file to the server." -ForegroundColor Yellow
