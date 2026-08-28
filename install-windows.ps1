param(
    [string]$Distro = "",
    [string]$RepoUrl = "https://github.com/malandr/telecli.git",
    [string]$Ref = "main",
    [string]$Prefix = '~/.local/share/telecli',
    [switch]$SkipSystemPackages
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "wsl.exe is not available. Install WSL first with: wsl --install -d Ubuntu"
}

$listedDistros = & wsl.exe -l -q 2>$null | Where-Object { $_.Trim() -ne "" }
if (-not $listedDistros) {
    throw "No WSL distro is installed. Install one first with: wsl --install -d Ubuntu"
}

$scriptUrl = "https://raw.githubusercontent.com/malandr/telecli/$Ref/scripts/install-wsl.sh"

Write-Step "Downloading WSL installer from $scriptUrl"
$wslInstaller = Invoke-WebRequest -UseBasicParsing -Uri $scriptUrl | Select-Object -ExpandProperty Content

$arguments = @()
if ($Distro) {
    $arguments += @("-d", $Distro)
}

$arguments += @(
    "--",
    "bash",
    "-s",
    "--",
    "--repo-url",
    $RepoUrl,
    "--ref",
    $Ref,
    "--prefix",
    $Prefix
)

if ($SkipSystemPackages) {
    $arguments += "--skip-system-packages"
}

Write-Step "Running TeleCLI installer inside WSL"
$wslInstaller | & wsl.exe @arguments

Write-Step "TeleCLI is installed in WSL"
Write-Host "Start it with: wsl telecli-wsl start"
Write-Host "Check status with: wsl telecli-wsl status"
Write-Host "Open logs with: wsl telecli-wsl logs"
