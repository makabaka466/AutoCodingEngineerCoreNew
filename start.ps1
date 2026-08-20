param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8501,
    [switch]$NoBrowser,
    [switch]$ForceInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$preferredPython = "D:\python\python.exe"

if (Test-Path -LiteralPath $preferredPython -PathType Leaf) {
    $pythonExe = $preferredPython
}
else {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }
    if ($null -eq $pythonCommand) {
        throw "Python was not found. Install Python 3.12+ or add python.exe to PATH."
    }
    $pythonExe = $pythonCommand.Source
}

& $pythonExe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "AutoCoding Engineer requires Python 3.12 or newer."
}

# Import user variables so configuration changes work without reopening the terminal.
$environmentNames = @(
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "AUTO_CODING_CLAUDE_COMMAND",
    "AUTO_CODING_CLAUDE_MODEL"
)
foreach ($name in $environmentNames) {
    $currentValue = [Environment]::GetEnvironmentVariable($name, "Process")
    if ([string]::IsNullOrWhiteSpace($currentValue)) {
        $userValue = [Environment]::GetEnvironmentVariable($name, "User")
        if (-not [string]::IsNullOrWhiteSpace($userValue)) {
            [Environment]::SetEnvironmentVariable($name, $userValue, "Process")
        }
    }
}

$env:PYTHONUTF8 = "1"
$sourcePath = Join-Path $projectRoot "src"
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $sourcePath
}
else {
    $env:PYTHONPATH = "$sourcePath;$($env:PYTHONPATH)"
}

Push-Location $projectRoot
try {
    & $pythonExe -c "import autocoding_agent, streamlit" 2>$null
    $dependenciesReady = $LASTEXITCODE -eq 0
    if ($ForceInstall -or -not $dependenciesReady) {
        Write-Host "Installing the project and UI dependencies..." -ForegroundColor Cyan
        & $pythonExe -m pip install -e ".[ui]"
        if ($LASTEXITCODE -ne 0) {
            throw "Project dependency installation failed."
        }
    }

    $hasAuthToken = -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_AUTH_TOKEN)
    $hasApiKey = -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_API_KEY)
    if (-not $hasAuthToken -and -not $hasApiKey) {
        Write-Warning "No model API key was detected. Run scripts\configure_deepseek.ps1 before submitting a task."
    }

    $uiFile = Join-Path $projectRoot "src\autocoding_agent\interfaces\streamlit_ui.py"
    $streamlitArgs = @(
        "-m",
        "streamlit",
        "run",
        $uiFile,
        "--server.port",
        $Port,
        "--browser.gatherUsageStats",
        "false"
    )
    if ($NoBrowser) {
        $streamlitArgs += @("--server.headless", "true")
    }

    Write-Host "Starting AutoCoding Engineer: http://localhost:$Port" -ForegroundColor Green
    Write-Host "Press Ctrl+C to stop the service."
    & $pythonExe @streamlitArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
