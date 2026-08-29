<#
.SYNOPSIS
    Set this application up on a Windows machine you reach over RDP.

.DESCRIPTION
    Does the repeatable parts and checks the rest: virtual environment,
    dependencies, a .env skeleton, an optional scheduled task that survives
    reboot and logoff, and an optional firewall rule.

    Safe to run more than once. It never overwrites an existing .env, and it
    reports what it found rather than assuming.

.PARAMETER InstallService
    Register a scheduled task so the application starts at boot without anyone
    logging in.

.PARAMETER OpenFirewall
    Allow inbound connections to the send API port. Prefer an SSH tunnel — see
    docs/GUIDE.md. Only meaningful with -ApiHost 0.0.0.0.

.PARAMETER ApiPort
    Port for the send API. Default 8766.

.EXAMPLE
    .\scripts\setup-remote.ps1
    .\scripts\setup-remote.ps1 -InstallService -OpenFirewall
#>
[CmdletBinding()]
param(
    [switch]$InstallService,
    [switch]$OpenFirewall,
    [int]$ApiPort = 8766
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text)  { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ok($text)    { Write-Host "   OK   $text" -ForegroundColor Green }
function Warn($text)  { Write-Host "   WARN $text" -ForegroundColor Yellow }
function Bad($text)   { Write-Host "   MISS $text" -ForegroundColor Red }

# ── prerequisites ───────────────────────────────────────────────────────
Step "Checking what is already here"

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    $version = (& python --version 2>&1) -replace 'Python\s*',''
    if ([version]($version -split '\+')[0] -ge [version]'3.11') { Ok "Python $version" }
    else { Bad "Python $version — 3.11 or newer is needed" }
} else {
    Bad "Python is not on PATH. Install 3.11+ from python.org, ticking 'Add to PATH'."
}

$mongo = Get-Service -Name MongoDB -ErrorAction SilentlyContinue
if ($mongo) {
    Ok "MongoDB service: $($mongo.Status), starts $($mongo.StartType)"
    if ($mongo.StartType -ne 'Automatic') {
        Warn "Set it to Automatic or it will not come back after a reboot:"
        Warn "  Set-Service MongoDB -StartupType Automatic"
    }
} else {
    Warn "No local MongoDB service. Fine if you are using Atlas — put the URI in .env."
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    try {
        docker info 2>&1 | Out-Null
        Ok "Docker is responding"
        $openwa = docker ps --filter "name=openwa" --format "{{.Names}} ({{.Status}})" 2>$null
        if ($openwa) { Ok "OpenWA container: $openwa" }
        else { Warn "No OpenWA container running. Start it before this application." }
    } catch { Bad "Docker is installed but not responding. Is Docker Desktop started?" }
} else {
    Bad "Docker is not on PATH — OpenWA runs in a container."
}

# The one that catches people out.
$dd = Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue
if ($dd) {
    Warn "Docker Desktop runs as YOUR login session. Disconnecting RDP is fine;"
    Warn "LOGGING OFF stops it, and OpenWA with it. See docs/GUIDE.md."
}

# ── the virtual environment ─────────────────────────────────────────────
Step "Virtual environment"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    python -m venv .venv
    Ok "created .venv"
} else {
    Ok ".venv already exists"
}
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
Ok "dependencies installed"

# ── configuration ───────────────────────────────────────────────────────
Step "Configuration"
$envPath = Join-Path $root ".env"
if (Test-Path $envPath) {
    Ok ".env exists — left untouched"
} else {
    $token = & $venvPython -c "import secrets; print(secrets.token_urlsafe(32))"
    @"
# Written by scripts/setup-remote.ps1. Two values are required; everything
# else is discovered at startup or has a working default.

MONGODB_URI=mongodb://localhost:27017

# From OpenWA's data/.api-key, or its dashboard.
OPENWA_API_KEY=

# Where incoming messages are POSTed, for chats with no URL of their own.
# DEFAULT_WEBHOOK=https://your.server/hook

# --- the send API -------------------------------------------------------
# Reachable from your own machine. The token is mandatory off loopback and is
# the only thing between the network and this WhatsApp account.
API_PORT=$ApiPort
API_HOST=0.0.0.0
API_TOKEN=$token
"@ | Set-Content -Path $envPath -Encoding UTF8
    Ok "wrote .env with a generated API token"
    Warn "Add OPENWA_API_KEY before starting — it is the one thing left."
}

# ── firewall ────────────────────────────────────────────────────────────
if ($OpenFirewall) {
    Step "Firewall"
    $name = "wadam send API ($ApiPort)"
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
        Ok "rule already present"
    } else {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP `
            -LocalPort $ApiPort -Action Allow | Out-Null
        Ok "opened TCP $ApiPort"
    }
    Warn "An open port sends your token in a header over plain HTTP."
    Warn "An SSH tunnel is better: ssh -L ${ApiPort}:localhost:$ApiPort you@thisbox"
}

# ── scheduled task ──────────────────────────────────────────────────────
if ($InstallService) {
    Step "Scheduled task"
    $task = "wadam"
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
        Ok "replaced the existing task"
    }
    $action = New-ScheduledTaskAction -Execute $venvPython `
        -Argument "run_headless.py" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # SYSTEM, so it runs with nobody logged in. RunOnlyIfIdle off, and restart
    # on failure, because the point is that it survives unattended.
    $settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest -User "SYSTEM" | Out-Null
    Ok "registered '$task' to run at startup as SYSTEM"
    Warn "It starts headless (no window). Start it now with: Start-ScheduledTask wadam"
}

# ── what is left ────────────────────────────────────────────────────────
Step "Next"
Write-Host @"
   1. OpenWA's own .env needs these two lines, then recreate its container:
        SSRF_ALLOWED_HOSTS=host.docker.internal
        WWEBJS_WEB_VERSION=<the build your session paired under>

   2. Link a session: open http://localhost:2785 in a browser ON THIS MACHINE
      and scan the QR. Only a phone can do this.

   3. Put OPENWA_API_KEY in .env (from OpenWA's data/.api-key).

   4. Start it:
        .venv\Scripts\python.exe run_headless.py     (or run.py for the window)

   5. Check it:
        Invoke-RestMethod http://localhost:8765/health

   Full walkthrough: docs/GUIDE.md
"@
