param(
    [string]$ProjectName = "tradespace-test",
    [string]$ComposeFile = "docker-compose.test.yml",
    [string[]]$ComposeArgs = @()
)

$ErrorActionPreference = "Stop"

$projectAllowed = (
    $ProjectName -eq "tradespace_test_run" -or
    $ProjectName -eq "tradespace-test" -or
    $ProjectName.StartsWith("tradespace-test-")
)
if (-not $projectAllowed) {
    Write-Host "REFUSED: test project is outside the cleanup allowlist: $ProjectName" -ForegroundColor Red
    exit 2
}

if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) {
    Write-Host "FAILED: compose file not found: $ComposeFile" -ForegroundColor Red
    exit 1
}

$upArgs = @(
    "-p", $ProjectName,
    "-f", $ComposeFile,
    "up",
    "--build",
    "--abort-on-container-exit",
    "--exit-code-from",
    "tests",
    "--remove-orphans"
)
$upArgs += $ComposeArgs

$dockerDownArgs = @(
    "-p", $ProjectName,
    "-f", $ComposeFile,
    "down",
    "--volumes",
    "--remove-orphans"
)

$testExitCode = 0

try {
    docker compose @upArgs
    $testExitCode = $LASTEXITCODE
} catch {
    Write-Host "Test workflow command failed: $($_.Exception.Message)" -ForegroundColor Red
    if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ge 0) {
        $testExitCode = $LASTEXITCODE
    } else {
        $testExitCode = 1
    }
} finally {
    try {
        docker compose @dockerDownArgs | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "WARNING: test workflow cleanup exited with code $LASTEXITCODE" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "WARNING: test workflow cleanup failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

exit $testExitCode
