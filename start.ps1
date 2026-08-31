param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8501,
    [switch]$Web,
    [switch]$NoBrowser,
    [switch]$ForceInstall,
    [switch]$Wait
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

# The configuration page persists these values for the Windows user. Always refresh the
# launcher process from that source so a long-lived Explorer/terminal cannot pass stale values.
$environmentNames = @(
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "AUTO_CODING_CLAUDE_COMMAND",
    "AUTO_CODING_CLAUDE_MODEL"
)
foreach ($name in $environmentNames) {
    $userValue = [Environment]::GetEnvironmentVariable($name, "User")
    if (-not [string]::IsNullOrWhiteSpace($userValue)) {
        [Environment]::SetEnvironmentVariable($name, $userValue, "Process")
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
    if ($Web) {
        & $pythonExe -c "import autocoding_agent, keyring, pyodbc, streamlit" 2>$null
    }
    else {
        & $pythonExe -c "import autocoding_agent, keyring, pyodbc, tkinter" 2>$null
    }
    $dependenciesReady = $LASTEXITCODE -eq 0
    if ($ForceInstall -or -not $dependenciesReady) {
        if ($Web) {
            $installTarget = ".[ui]"
        }
        else {
            $installTarget = "."
        }
        Write-Host "Installing AutoCoding Engineer dependencies..." -ForegroundColor Cyan
        & $pythonExe -m pip install -e $installTarget
        if ($LASTEXITCODE -ne 0) {
            throw "Project dependency installation failed."
        }
    }

    if (-not $Web) {
        & $pythonExe -c "import tkinter; from autocoding_agent.interfaces.desktop_ui import main" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "The desktop client could not be loaded. Ensure this Python installation includes tkinter."
        }
    }

    if (-not $Web) {
        Write-Host "Starting the AutoCoding Engineer desktop client..." -ForegroundColor Green
        if ($Wait) {
            & $pythonExe -m autocoding_agent.interfaces.desktop_ui
            exit $LASTEXITCODE
        }

        $pythonwExe = Join-Path (Split-Path -Parent $pythonExe) "pythonw.exe"
        if (-not (Test-Path -LiteralPath $pythonwExe -PathType Leaf)) {
            $pythonwExe = $pythonExe
        }
        $clientProcess = Start-Process `
            -FilePath $pythonwExe `
            -ArgumentList @("-m", "autocoding_agent.interfaces.desktop_ui") `
            -WorkingDirectory $projectRoot `
            -WindowStyle Normal `
            -PassThru
        Start-Sleep -Milliseconds 800
        if ($clientProcess.HasExited -and $clientProcess.ExitCode -ne 0) {
            throw "The desktop client exited during startup with code $($clientProcess.ExitCode)."
        }
        Write-Host "Desktop client started (PID $($clientProcess.Id))."
        exit 0
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

    Write-Host "Starting the optional Web UI: http://localhost:$Port" -ForegroundColor Green
    Write-Host "Press Ctrl+C to stop the Web service."
    & $pythonExe @streamlitArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
