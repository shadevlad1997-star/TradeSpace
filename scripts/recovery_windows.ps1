param([ValidateSet('Start', 'Stop', 'Status', 'StopApplications')][string]$Action = 'Start')
$ErrorActionPreference = 'Stop'
$recoveryRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $recoveryRoot
$recoveryPython = Join-Path $recoveryRoot '.venv\Scripts\python.exe'
$recoveryPg = Join-Path $recoveryRoot 'tmp\runtime\postgresql\pgsql\bin\pg_ctl.exe'
$recoveryRedis = Join-Path $recoveryRoot 'tmp\runtime\redis\Redis-7.4.11-Windows-x64-cygwin\redis-server.exe'
$recoveryLocalRoot = if ($env:TRADESPACE_LOCAL_DIR) { $env:TRADESPACE_LOCAL_DIR } else { Join-Path (Split-Path -Parent $recoveryRoot) 'TradeSpace-local' }
$recoveryEnvFile = Join-Path $recoveryLocalRoot '.env'
$recoveryRecords = Join-Path $recoveryLocalRoot 'run\services.json'
$dataSetting = Get-Content -LiteralPath $recoveryEnvFile | Where-Object { $_ -match '^TRADESPACE_DATA_DIR=' } | Select-Object -First 1
$recoveryDataRoot = if ($dataSetting) { $dataSetting.Substring('TRADESPACE_DATA_DIR='.Length) } else { Join-Path (Split-Path -Parent $recoveryRoot) 'TradeSpaceData' }
$recoveryPgData = Join-Path $recoveryDataRoot 'postgres'

function Read-RecoveryProcesses {
    if (Test-Path -LiteralPath $recoveryRecords) {
        return @(Get-Content -LiteralPath $recoveryRecords -Raw | ConvertFrom-Json)
    }
    return @()
}

function Get-RecoveryProcess($record) {
    $candidate = Get-Process -Id $record.Id -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.StartTime.ToUniversalTime().Ticks -eq ([datetime]$record.Started).ToUniversalTime().Ticks) {
        return $candidate
    }
    return $null
}

if ($Action -eq 'Status') {
    foreach ($record in (Read-RecoveryProcesses)) {
        [pscustomobject]@{ Service=$record.Name; PID=$record.Id; Running=[bool](Get-RecoveryProcess $record) }
    }
    & $recoveryPython -m scripts.recovery_run smoke
    exit $LASTEXITCODE
}

if ($Action -in @('Stop', 'StopApplications')) {
    foreach ($record in (Read-RecoveryProcesses)) {
        if ($record.Name -ne 'redis') {
            $candidate = Get-RecoveryProcess $record
            # The recorded PID is the venv launcher. Killing its tree also
            # stops the base Python child that runs uvicorn or Celery.
            if ($candidate) { & taskkill.exe /PID $candidate.Id /T /F | Out-Null }
        }
    }
    & $recoveryPython -c "from scripts.recovery_run import local_environment; from redis import Redis; Redis.from_url(local_environment()['REDIS_URL'], socket_connect_timeout=1).shutdown(save=True)" 2>$null
    if ($Action -eq 'StopApplications') {
        Remove-Item -LiteralPath $recoveryRecords -Force -ErrorAction SilentlyContinue
        exit 0
    }
    & $recoveryPg -D $recoveryPgData status *> $null
    if ($LASTEXITCODE -eq 0) {
        & $recoveryPg -D $recoveryPgData -m fast -w stop
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not stop' }
    }
    Remove-Item -LiteralPath $recoveryRecords -Force -ErrorAction SilentlyContinue
    exit 0
}

# Validate endpoint isolation before loading credentials or launching processes.
& $recoveryPython -c "from scripts.recovery_run import local_environment; local_environment()"
if ($LASTEXITCODE -ne 0) { throw 'Invalid local environment' }
foreach ($line in (Get-Content -LiteralPath $recoveryEnvFile)) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
& $recoveryPg -D $recoveryPgData status *> $null
if ($LASTEXITCODE -ne 0) {
    & $recoveryPg -D $recoveryPgData -l 'logs\postgres.log' -o '-h 127.0.0.1 -p 55432' -w start
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not start' }
}
$records = @(Read-RecoveryProcesses)
function Start-RecoveryService($name, $exe, $arguments) {
    $previous = $records | Where-Object Name -eq $name | Select-Object -First 1
    if ($previous -and (Get-RecoveryProcess $previous)) { return $previous }
    $process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $recoveryRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "logs\$name.log" -RedirectStandardError "logs\$name-error.log"
    Start-Sleep -Milliseconds 750
    if ($process.HasExited) { throw "$name stopped during startup; inspect logs\$name-error.log" }
    # The venv launcher stays alive as the parent of the base Python process.
    # Recording the launcher lets taskkill /T stop the complete service tree.
    return [pscustomobject]@{ Name=$name; Id=$process.Id; Started=$process.StartTime.ToUniversalTime().ToString('o') }
}
& $recoveryPython -c "from redis import Redis; import os,sys
try: Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=1).ping()
except Exception: sys.exit(1)"
$newRecords = @()
if ($LASTEXITCODE -ne 0) {
    $newRecords += Start-RecoveryService 'redis' $recoveryRedis ('/cygdrive/' + $recoveryLocalRoot.Substring(0,1).ToLower() + $recoveryLocalRoot.Substring(2).Replace('\','/') + '/run/redis.conf')
} else {
    # This Cygwin Redis build reports a POSIX PID in INFO, not the Windows PID.
    $redisProcess = Get-Process -Name 'redis-server' -ErrorAction Stop |
        Where-Object { $_.Path -eq $recoveryRedis } |
        Select-Object -First 1
    if (-not $redisProcess) { throw 'Redis answered PING but its Windows process was not found' }
    $newRecords += [pscustomobject]@{ Name='redis'; Id=$redisProcess.Id; Started=$redisProcess.StartTime.ToUniversalTime().ToString('o') }
}
$newRecords += Start-RecoveryService 'api' $recoveryPython '-m uvicorn app.main:app --host 127.0.0.1 --port 8000'
$newRecords += Start-RecoveryService 'worker' $recoveryPython '-m celery -A app.workers.celery_app worker --pool=solo --concurrency=1 --loglevel=INFO --hostname=tradespace-local@%h'
$beatPidPath = Join-Path $recoveryLocalRoot 'run\celerybeat.pid'
if (Test-Path -LiteralPath $beatPidPath) {
    $beatPid = [int](Get-Content -LiteralPath $beatPidPath -Raw)
    if (-not (Get-Process -Id $beatPid -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $beatPidPath -Force
    }
}
$newRecords += Start-RecoveryService 'beat' $recoveryPython "-m celery -A app.workers.celery_app beat --loglevel=INFO --schedule=$recoveryLocalRoot/run/celerybeat-schedule --pidfile=$recoveryLocalRoot/run/celerybeat.pid"
$newRecords += Start-RecoveryService 'webhook-receiver' $recoveryPython '-m scripts.recovery_webhook'
$newRecords | ConvertTo-Json | Set-Content -LiteralPath $recoveryRecords -Encoding UTF8
$newRecords | Select-Object Name,Id



# Derive the final result from current services and HTTP, not the intentional
# failed Redis probe that precedes starting a stopped instance.
foreach ($record in $newRecords) {
    if (-not (Get-RecoveryProcess $record)) { throw "Service exited during startup: $($record.Name)" }
}
& $recoveryPython -m scripts.recovery_run smoke
$startupSmokeResult = $LASTEXITCODE
if ($startupSmokeResult -ne 0) { throw "Started processes failed HTTP smoke: $startupSmokeResult" }
exit $startupSmokeResult
