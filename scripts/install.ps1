#Requires -Version 5.1
<#
.SYNOPSIS
    One-line Windows PowerShell installer for agentsync.
.DESCRIPTION
    Installs agentsync globally using uv, pipx, or pip (in priority order),
    verifies PATH exposure, and confirms `agentsync --version` executes cleanly.
#>
param(
    [string]$RepoUrl = "git+https://github.com/saleqss/agentsync.git"
)

$ErrorActionPreference = "Stop"

Write-Host "[agentsync] Starting installation from $RepoUrl..." -ForegroundColor Cyan

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "[agentsync] Installing via uv tool..." -ForegroundColor Green
    uv tool install --force $RepoUrl
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Host "[agentsync] Installing via pipx..." -ForegroundColor Green
    pipx install --force $RepoUrl
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "[agentsync] Installing via python -m pip --user..." -ForegroundColor Green
    python -m pip install --user --upgrade $RepoUrl
    $userScripts = python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
    if ($userScripts -and (Test-Path $userScripts)) {
        $currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not $currentUserPath -or ($currentUserPath -notlike "*$userScripts*")) {
            Write-Host "[agentsync] Adding '$userScripts' to User PATH..." -ForegroundColor Yellow
            $newPath = if ($currentUserPath) { "$currentUserPath;$userScripts" } else { $userScripts }
            [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
            $env:Path = "$env:Path;$userScripts"
        }
    }
} else {
    Write-Error "Python 3.11+, pipx, or uv is required to install agentsync."
    exit 1
}

$cmd = Get-Command agentsync -ErrorAction SilentlyContinue
if (-not $cmd) {
    Write-Warning "agentsync was installed, but is not yet on the current shell PATH. Open a new terminal and run 'agentsync --version'."
    exit 0
}

Write-Host "[agentsync] Verifying global command..." -ForegroundColor Cyan
& $cmd.Source --version
Write-Host "[agentsync] Installation complete! Run 'agentsync discover --save' to get started." -ForegroundColor Green
