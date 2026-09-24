param(
    [string]$ProjectPath = ".",
    [string]$EnvFile = ".env.production",
    [string]$ProjectName = "processing_platform",
    [switch]$SkipPreflight,
    [switch]$SkipTls,
    [switch]$BackupVerified,
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"

$project = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ProjectPath)
Set-Location -LiteralPath $project
$envPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EnvFile)

if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    Write-Host "FAILED: env file not found: $envPath" -ForegroundColor Red
    exit 1
}

if (-not $SkipPreflight) {
    $preflightArgs = @("-ExecutionPolicy", "Bypass", "-File", ".\scripts\server_preflight.ps1", "-ProjectPath", $project, "-EnvFile", $envPath)
    if ($SkipTls) {
        $preflightArgs += "-SkipTls"
    }
    & powershell @preflightArgs
}

$commands = @(
    "docker compose -p $ProjectName --env-file `"$envPath`" -f docker-compose.production.yml config --quiet",
    "docker compose -p $ProjectName --env-file `"$envPath`" -f docker-compose.production.yml stop api worker beat",
    "docker compose -p $ProjectName --env-file `"$envPath`" -f docker-compose.production.yml --profile migrate run --rm migrate",
    "docker compose -p $ProjectName --env-file `"$envPath`" -f docker-compose.production.yml up -d --build api worker beat",
    "docker compose -p $ProjectName --env-file `"$envPath`" -f docker-compose.production.yml up -d nginx",
    "docker compose -p $ProjectName --env-file `"$envPath`" -f docker-compose.production.yml exec nginx nginx -s reload",
    "docker compose -p $ProjectName --env-file `"$envPath`" -f docker-compose.production.yml ps"
)

if ($PlanOnly) {
    Write-Host "Production deploy plan:" -ForegroundColor Cyan
    foreach ($command in $commands) {
        Write-Host $command
    }
    Write-Host "Plan only mode completed. Nothing was deployed." -ForegroundColor Yellow
    exit 0
}

if (-not $BackupVerified) {
    Write-Host "REFUSED: verify the encrypted backup and pass -BackupVerified." -ForegroundColor Red
    exit 2
}

$oldAppEnvFile = $env:APP_ENV_FILE
$env:APP_ENV_FILE = $envPath
try {
    docker compose -p $ProjectName --env-file $envPath -f docker-compose.production.yml config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Production Compose config validation failed." }

    docker compose -p $ProjectName --env-file $envPath -f docker-compose.production.yml stop api worker beat
    if ($LASTEXITCODE -ne 0) { throw "Failed to stop application writers." }

    docker compose -p $ProjectName --env-file $envPath -f docker-compose.production.yml --profile migrate run --rm migrate
    if ($LASTEXITCODE -ne 0) {
        throw "Migration job failed. Application services were not started."
    }

    docker compose -p $ProjectName --env-file $envPath -f docker-compose.production.yml up -d --build api worker beat
    if ($LASTEXITCODE -ne 0) { throw "Application service startup failed." }

    docker compose -p $ProjectName --env-file $envPath -f docker-compose.production.yml up -d nginx
    if ($LASTEXITCODE -ne 0) { throw "Nginx startup failed." }

    docker compose -p $ProjectName --env-file $envPath -f docker-compose.production.yml exec nginx nginx -s reload
    if ($LASTEXITCODE -ne 0) { throw "Nginx reload failed." }

    docker compose -p $ProjectName --env-file $envPath -f docker-compose.production.yml ps
    if ($LASTEXITCODE -ne 0) { throw "Unable to read production service status." }
} finally {
    if ($null -eq $oldAppEnvFile) {
        Remove-Item Env:APP_ENV_FILE -ErrorAction SilentlyContinue
    } else {
        $env:APP_ENV_FILE = $oldAppEnvFile
    }
}

Write-Host "Production deployment command completed." -ForegroundColor Green
Write-Host "Run post-deploy checks next:" -ForegroundColor Cyan
Write-Host "powershell -ExecutionPolicy Bypass -File .\scripts\post_deploy_check.ps1 -BaseUrl `"https://your-domain.example`""
