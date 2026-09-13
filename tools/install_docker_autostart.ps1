param(
    [string]$ProjectRoot = (Split-Path $PSScriptRoot -Parent),
    [switch]$Uninstall,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$startupFolder = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
$shortcutPath = Join-Path $startupFolder "Vision Office Docker Startup.lnk"

if ($Uninstall) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "Removed Windows sign-in autostart for Vision Office." -ForegroundColor Green
    }
    else {
        Write-Host "Vision Office autostart is not installed for this Windows user." -ForegroundColor Yellow
    }
    exit 0
}

$launcher = Join-Path $ProjectRoot "launcher\VisionOfficeAutostart.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Launcher not found: $launcher. Run tools\\build_autostart_launcher.ps1 first."
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.Arguments = "--project-root `"$ProjectRoot`""
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.IconLocation = "$launcher,0"
$shortcut.Description = "Starts Vision Office Docker services after Windows sign-in"
$shortcut.Save()
Write-Host "Installed Windows sign-in autostart for Vision Office." -ForegroundColor Green

if ($RunNow) {
    Start-Process -FilePath $launcher -ArgumentList @("--project-root", $ProjectRoot) -WorkingDirectory $ProjectRoot -WindowStyle Hidden
    Write-Host "Autostart helper started. Follow data\\logs\\docker-autostart.log." -ForegroundColor Green
}
