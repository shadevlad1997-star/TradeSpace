param(
    [string]$ProjectPath = ".",
    [string]$EnvFile = ".env.production",
    [switch]$SkipTls,
    [switch]$SkipPortCheck,
    [switch]$AllowExampleEnv
)

$ErrorActionPreference = "Stop"

function Resolve-ProjectPath {
    param([string]$Path)
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Write-Ok {
    param([string]$Message)
    Write-Host "OK: $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "FAILED: $Message" -ForegroundColor Red
    exit 1
}

function Test-CommandAvailable {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Write-Fail "$Name is not installed or not in PATH"
    }
    Write-Ok "$Name is available"
}

$project = Resolve-ProjectPath -Path $ProjectPath
if (-not (Test-Path -LiteralPath $project -PathType Container)) {
    Write-Fail "project path does not exist: $project"
}
Set-Location -LiteralPath $project

$requiredFiles = @(
    "docker-compose.production.yml",
    "nginx/production.conf",
    "scripts/security_check.ps1",
    "scripts/security_acceptance.ps1",
    "scripts/health_check.ps1"
)
foreach ($file in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        Write-Fail "required file missing: $file"
    }
    Write-Ok "found $file"
}

$envPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EnvFile)
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    Write-Fail "env file not found: $envPath"
}
Write-Ok "env file found: $envPath"

Test-CommandAvailable -Name "docker"
docker version | Out-Null
Write-Ok "Docker daemon is reachable"

docker compose version | Out-Null
Write-Ok "Docker Compose is available"

if (-not $SkipTls) {
    foreach ($certFile in @("nginx/certs/fullchain.pem", "nginx/certs/privkey.pem")) {
        if (-not (Test-Path -LiteralPath $certFile -PathType Leaf)) {
            Write-Fail "TLS file missing: $certFile"
        }
        Write-Ok "TLS file found: $certFile"
    }
} else {
    Write-Warn "TLS file check skipped"
}

if (-not $SkipPortCheck) {
    foreach ($port in @(80, 443)) {
        try {
            $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
            if ($listeners) {
                Write-Warn "port $port already has a listener; Nginx may fail to bind it"
            } else {
                Write-Ok "port $port is free"
            }
        } catch {
            Write-Warn "could not check port ${port}: $($_.Exception.Message)"
        }
    }
} else {
    Write-Warn "port check skipped"
}

try {
    $drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($project).Substring(0, 1))
    $freeGb = [math]::Round($drive.Free / 1GB, 2)
    if ($freeGb -lt 10) {
        Write-Warn "free disk space is low: $freeGb GB"
    } else {
        Write-Ok "free disk space: $freeGb GB"
    }
} catch {
    Write-Warn "could not check disk space: $($_.Exception.Message)"
}

$securityArgs = @("-ExecutionPolicy", "Bypass", "-File", ".\scripts\security_check.ps1", "-EnvFile", $envPath)
if ($AllowExampleEnv) {
    $securityArgs += "-AllowExample"
}
& powershell @securityArgs
Write-Ok "security env check passed"

$oldAppEnvFile = $env:APP_ENV_FILE
$env:APP_ENV_FILE = $envPath
try {
    docker compose --env-file $envPath -f docker-compose.production.yml config --quiet
    Write-Ok "production compose config is valid"
} finally {
    if ($null -eq $oldAppEnvFile) {
        Remove-Item Env:APP_ENV_FILE -ErrorAction SilentlyContinue
    } else {
        $env:APP_ENV_FILE = $oldAppEnvFile
    }
}

Write-Host "Server preflight completed." -ForegroundColor Green
