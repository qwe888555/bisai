<#
.SYNOPSIS
    One-click deploy for the football platform (frontend dist + backend code).

.DESCRIPTION
    Runs from a Windows Git Bash / PowerShell shell. Uses the OpenSSH client and
    tar that ship with Git for Windows, so no extra tooling is required.

    Modes:
      init    Connect once and print what is on the server (nginx roots, running
              services, manage.py locations). Use the output to fill the config.
      deploy  Build the frontend, upload dist, pull the backend and restart it.
      check   Only run the remote pull/restart and the health check.

    Auth: key-based is strongly recommended (ssh-copy-id user@host). Without a
    key every ssh/scp call asks for the password again.

.EXAMPLE
    .\scripts\deploy.ps1 -Mode init
    .\scripts\deploy.ps1 -Mode deploy
    .\scripts\deploy.ps1 -Mode deploy -SkipFrontend
#>
[CmdletBinding()]
param(
    [ValidateSet('init', 'deploy', 'check')]
    [string]$Mode = 'deploy',
    [switch]$SkipFrontend,
    [switch]$SkipBackend
)

$ErrorActionPreference = 'Stop'

$ScriptDir   = $PSScriptRoot
$RepoRoot    = Split-Path -Parent $ScriptDir
$WorkRoot    = Split-Path -Parent $RepoRoot
$FrontendDir = Join-Path $WorkRoot 'zuqiu'
$ConfigPath  = Join-Path $ScriptDir 'deploy.config.json'

function Resolve-Tool([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        'E:\sunmmer Lab\Git\usr\bin\{0}.exe' -f $Name,
        'C:\Program Files\Git\usr\bin\{0}.exe' -f $Name,
        'C:\Program Files\Git\bin\{0}.exe' -f $Name
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    throw "Cannot find '$Name'. Install Git for Windows or add it to PATH."
}

$Ssh = Resolve-Tool 'ssh'
$Scp = Resolve-Tool 'scp'
$Tar = Resolve-Tool 'tar'

function Read-Config {
    if (-not (Test-Path $ConfigPath)) {
        throw "Missing $ConfigPath. Run: .\scripts\deploy.ps1 -Mode init  (then fill it in)"
    }
    return Get-Content $ConfigPath -Raw | ConvertFrom-Json
}

function Invoke-Remote([string]$RemoteCommand, [switch]$AllowFail) {
    & $Ssh -p $script:Cfg.port -o StrictHostKeyChecking=accept-new `
        "$($script:Cfg.user)@$($script:Cfg.host)" $RemoteCommand
    if ($LASTEXITCODE -ne 0 -and -not $AllowFail) {
        throw "Remote command failed (exit $LASTEXITCODE): $RemoteCommand"
    }
}

function Invoke-Init {
    Write-Host '== Server discovery ==' -ForegroundColor Cyan
    $probe = @'
echo "OS: $(. /etc/os-release 2>/dev/null; echo $PRETTY_NAME)"
echo "--- listening 8082 ---"
(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep ':8082' || echo "port 8082 not bound"
echo "--- services ---"
systemctl list-units --type=service --no-pager --plain 2>/dev/null | grep -Ei 'football|django|gunicorn|uwsgi|uvicorn|celery' || echo "no matching service"
echo "--- nginx roots ---"
grep -rhn -E 'listen\s+8082|server_name|root\s+/' /etc/nginx/conf.d /etc/nginx/sites-enabled /www/server/panel/vhost/nginx 2>/dev/null | head -40 || echo "no nginx config"
echo "--- www roots ---"
ls -d /www/wwwroot/* /opt/* /srv/* 2>/dev/null | head -20
echo "--- manage.py ---"
find /www /opt /srv /root -maxdepth 4 -name manage.py 2>/dev/null | head -10
'@
    Invoke-Remote $probe -AllowFail

    if (-not (Test-Path $ConfigPath)) {
        $template = [ordered]@{
            host     = '111.228.56.207'
            port     = 22
            user     = 'root'
            frontend = [ordered]@{ remoteDir = '/www/wwwroot/football/dist'; buildCmd = 'npm run build' }
            backend  = [ordered]@{ remoteDir = '/www/wwwroot/football/backend'; restart = 'systemctl restart football' }
            health   = 'http://111.228.56.207:8082/'
        }
        $template | ConvertTo-Json -Depth 5 |
            Set-Content -Path $ConfigPath -Encoding UTF8
        Write-Host "`nTemplate written to $ConfigPath" -ForegroundColor Yellow
        Write-Host 'Fill in remoteDir / restart from the discovery output above.' -ForegroundColor Yellow
    }
    else {
        Write-Host "`nConfig already exists: $ConfigPath" -ForegroundColor Yellow
    }
}

function Deploy-Frontend {
    if (-not (Test-Path $FrontendDir)) { throw "Frontend repo not found: $FrontendDir" }
    Write-Host '== Build frontend ==' -ForegroundColor Cyan
    Push-Location $FrontendDir
    try {
        $buildCmd = if ($script:Cfg.frontend.buildCmd) { $script:Cfg.frontend.buildCmd } else { 'npm run build' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed - nothing was uploaded.' }

        $dist = Join-Path $FrontendDir 'dist'
        if (-not (Test-Path $dist)) { throw "dist not found after build: $dist" }

        $pkg = Join-Path $env:TEMP 'football-dist.tgz'
        if (Test-Path $pkg) { Remove-Item $pkg -Force }
        Write-Host '== Pack dist ==' -ForegroundColor Cyan
        & $Tar -czf $pkg -C $dist .
        if ($LASTEXITCODE -ne 0) { throw 'tar failed.' }

        $remote = $script:Cfg.frontend.remoteDir
        Write-Host "== Upload to $remote ==" -ForegroundColor Cyan
        & $Scp -P $script:Cfg.port -o StrictHostKeyChecking=accept-new $pkg `
            "$($script:Cfg.user)@$($script:Cfg.host):/tmp/football-dist.tgz"
        if ($LASTEXITCODE -ne 0) { throw 'Upload failed.' }

        Invoke-Remote "set -e; rm -rf '$remote.bak'; if [ -d '$remote' ]; then mv '$remote' '$remote.bak'; fi; mkdir -p '$remote'; tar -xzf /tmp/football-dist.tgz -C '$remote'; rm -f /tmp/football-dist.tgz; echo 'frontend updated'"
    }
    finally { Pop-Location }
}

function Deploy-Backend {
    $dir = $script:Cfg.backend.remoteDir
    $restart = $script:Cfg.backend.restart
    Write-Host '== Backend pull + restart ==' -ForegroundColor Cyan
    Invoke-Remote "set -e; cd '$dir'; git fetch --quiet; git pull --ff-only; $restart; sleep 3; curl -s -o /dev/null -w 'backend docs: %{http_code}\n' http://127.0.0.1:8000/api/v1/docs || true"
}

function Test-Health {
    if (-not $script:Cfg.health) { return }
    Write-Host '== Health check ==' -ForegroundColor Cyan
    try {
        $r = Invoke-WebRequest -Uri $script:Cfg.health -UseBasicParsing -TimeoutSec 15
        Write-Host ("site: {0} ({1} bytes)" -f $r.StatusCode, $r.RawContentLength) -ForegroundColor Green
    }
    catch {
        Write-Host ("site check failed: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
}

if ($Mode -eq 'init') {
    Invoke-Init
    return
}

$script:Cfg = Read-Config
Write-Host ("Target: {0}@{1}:{2}" -f $script:Cfg.user, $script:Cfg.host, $script:Cfg.port) -ForegroundColor Cyan

if ($Mode -eq 'deploy' -and -not $SkipFrontend) { Deploy-Frontend }
if (-not $SkipBackend) { Deploy-Backend }
Test-Health
Write-Host 'Done.' -ForegroundColor Green
