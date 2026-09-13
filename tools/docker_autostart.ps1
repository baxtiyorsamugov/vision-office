<#
Starts the already configured Vision Office Docker stack after Windows sign-in.
It intentionally does not build images or select GPU/CPU: those operations belong
to the interactive start_vision_office.ps1 maintenance launcher.
#>
param(
    [string]$ProjectRoot = (Split-Path $PSScriptRoot -Parent),
    [ValidateRange(10, 900)][int]$TimeoutSeconds = 300,
    [switch]$CheckOnly,
    [switch]$NoDesktopStart,
    [string]$DockerExecutable,
    [string]$DockerDesktopExecutable
)

$ErrorActionPreference = "Stop"
$script:exitCode = 1
$mutex = $null
$ownsMutex = $false

function Get-FirstExistingPath {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Protect-LogText {
    param([string]$Text)
    $value = $Text -replace '(?i)(rtsp|https?)://[^\s/@:]+:[^\s/@]+@', '$1://***:***@'
    $value = $value -replace '(?i)(authorization\s*[:=]\s*(bearer\s+)?)\S+', '$1***'
    return ($value -replace '(?i)(token|password|api[_-]?key)\s*[:=]\s*\S+', '$1=***')
}

function Write-AutostartLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [docker-autostart] $(Protect-LogText $Message)$([Environment]::NewLine)"
    $directory = Split-Path $script:logPath -Parent
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    if ((Test-Path -LiteralPath $script:logPath) -and ((Get-Item -LiteralPath $script:logPath).Length -gt 2MB)) {
        Move-Item -LiteralPath $script:logPath -Destination "$($script:logPath).1" -Force
    }
    [IO.File]::AppendAllText($script:logPath, $line, (New-Object Text.UTF8Encoding($false)))
}

function Test-DockerDaemon {
    $serverOs = @(& $script:dockerExe version --format '{{.Server.Os}}' 2>$null)
    return $LASTEXITCODE -eq 0 -and (($serverOs -join '').Trim().ToLowerInvariant() -eq 'linux')
}

function Invoke-Compose {
    param([string[]]$Arguments, [string]$FailureMessage)
    & $script:dockerExe compose @Arguments 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
    $script:logPath = Join-Path $ProjectRoot "data\logs\docker-autostart.log"
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "docker-compose.yml") -PathType Leaf)) {
        throw "docker-compose.yml was not found in the selected project folder."
    }

    $script:dockerExe = Get-FirstExistingPath @(
        $DockerExecutable,
        (Get-Command docker.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
    )
    if (-not $script:dockerExe) {
        throw "Docker CLI was not found. Install Docker Desktop, then sign in to Windows again."
    }
    $desktopExe = Get-FirstExistingPath @(
        $DockerDesktopExecutable,
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )

    if ($CheckOnly) {
        Write-AutostartLog "Check passed: project and Docker CLI are available. Docker Desktop was not started."
        Write-Output "Vision Office autostart check passed."
        $script:exitCode = 0
        return
    }

    $mutex = [Threading.Mutex]::new($false, "VisionOfficeDockerAutostart")
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) {
        Write-AutostartLog "Another autostart instance is already running; this instance exits."
        $script:exitCode = 0
        return
    }

    if (-not (Test-DockerDaemon)) {
        if ($NoDesktopStart -or -not $desktopExe) {
            throw "Docker Desktop is not ready and could not be started automatically."
        }
        Write-AutostartLog "Docker Desktop is not ready; starting it and waiting up to $TimeoutSeconds seconds."
        Start-Process -FilePath $desktopExe -WindowStyle Hidden
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
            if (Test-DockerDaemon) { break }
        }
    }
    if (-not (Test-DockerDaemon)) {
        throw "Docker Desktop did not become ready within $TimeoutSeconds seconds."
    }

    Push-Location $ProjectRoot
    try {
        Invoke-Compose -Arguments @('config', '--quiet') -FailureMessage "Docker Compose configuration is invalid."
        Write-AutostartLog "Docker is ready; starting the saved Vision Office services."
        Invoke-Compose -Arguments @('up', '-d', '--no-build') -FailureMessage "Docker Compose could not start Vision Office. Run docker compose logs from the project folder."
    }
    finally {
        Pop-Location
    }
    Write-AutostartLog "Startup command completed. Dashboard: http://127.0.0.1:8501"
    $script:exitCode = 0
}
catch {
    if ($script:logPath) {
        Write-AutostartLog "Startup failed: $($_.Exception.Message)"
    }
    [Console]::Error.WriteLine("Vision Office autostart failed. See data\\logs\\docker-autostart.log.")
}
finally {
    if ($ownsMutex -and $mutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}

exit $script:exitCode
