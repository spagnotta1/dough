<#
.SYNOPSIS
    Push this installation's configuration into a linked Railway service.

.DESCRIPTION
    Every secret is read from the file that already holds it and piped straight
    to the Railway CLI. No value is ever echoed, written to a second file, or
    passed on a command line that lands in shell history -- which matters most
    for ENCRYPTION_KEY.

    ENCRYPTION_KEY is the Fernet key protecting `connected_accounts.auth_blob`,
    the live Plaid access tokens. This script copies the existing key out of
    `.sync_encryption_key` rather than generating one. A fresh key does not fail
    loudly: the application boots, and the four stored connections become
    permanently undecryptable at the next sync. There is no recovery other than
    re-linking every institution.

    SECRET_KEY is generated fresh, deliberately. It only signs session cookies,
    so a new one costs a single re-login, and reusing the development key would
    mean a copy of this working tree could forge production sessions.

.EXAMPLE
    railway login
    railway link
    .\tools\set_railway_env.ps1 -PublicBaseUrl https://dough-production.up.railway.app
#>
[CmdletBinding()]
param(
    # This service's canonical URL. Without it, links in verification and
    # password-reset mail are built from the incoming Host header, which the
    # client controls.
    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl,

    # Where the Railway volume is mounted. Must match the mount path configured
    # on the volume in the dashboard.
    [string]$VolumePath = '/data',

    # Print what would be set, touching nothing.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

# Normalised so the DATABASE_URL below is built from a known shape. A relative
# mount path would produce a three-slash sqlite:// URL, which SQLAlchemy reads
# as relative to the working directory -- a database inside the image, silently
# discarded by the next deploy.
$VolumePath = '/' + $VolumePath.Trim().Trim('/')

if (-not (Get-Command railway -ErrorAction SilentlyContinue)) {
    throw "The Railway CLI is not on PATH. Install it with 'npm i -g @railway/cli', then run 'railway login' and 'railway link'."
}

# The CLI renamed this command between major versions and both spellings are
# still in the wild. Detect once rather than guessing and half-configuring the
# service: a script that sets six of nine variables and then errors leaves a
# deployment that boots and behaves strangely.
$script:SetForm = $null
function Resolve-SetForm {
    foreach ($form in @('variables', 'variable')) {
        & railway $form --help *> $null
        if ($LASTEXITCODE -eq 0) { return $form }
    }
    throw "Neither 'railway variables' nor 'railway variable' is available. Check 'railway --version' and that 'railway link' has been run."
}

function Set-RailwayVar {
    param([string]$Name, [string]$Value, [switch]$Secret)

    $shown = if ($Secret) { '<hidden>' } else { $Value }
    if ($DryRun) {
        Write-Host ("  {0,-22} {1}" -f $Name, $shown)
        return
    }
    if (-not $script:SetForm) { $script:SetForm = Resolve-SetForm }

    if ($script:SetForm -eq 'variables') {
        & railway variables --set "$Name=$Value" *> $null
    } else {
        & railway variable set "$Name=$Value" *> $null
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to set $Name (railway exited $LASTEXITCODE)." }
    Write-Host ("  {0,-22} {1}" -f $Name, $shown)
}

function Read-KeyFile {
    param([string]$Path, [string]$What)
    if (-not (Test-Path $Path)) {
        throw "$What not found at $Path. This installation has encrypted data that depends on it; do not continue with a generated key."
    }
    $value = (Get-Content -Raw -Path $Path).Trim()
    if (-not $value) { throw "$What at $Path is empty." }
    return $value
}

# --- Secrets carried over from this installation -----------------------------
$encryptionKey = Read-KeyFile -Path (Join-Path $repo '.sync_encryption_key') -What 'The Fernet encryption key'

# --- Secrets generated for this deployment -----------------------------------
$secretKey = & python -c "import secrets; print(secrets.token_hex(32))"
if ($LASTEXITCODE -ne 0 -or -not $secretKey) { throw 'Could not generate SECRET_KEY; is python on PATH?' }

# --- Feature credentials, read from the local .env ---------------------------
# Absent ones are skipped rather than set empty: config.py treats an unset
# ANTHROPIC_API_KEY as "the AI surfaces are off", which is a valid deployment,
# and an empty PLAID pair as "run the sandbox". Setting them blank would be the
# same thing with more noise.
$fromEnv = @{}
$envPath = Join-Path $repo '.env'
if (Test-Path $envPath) {
    $wanted = @('ANTHROPIC_API_KEY', 'PLAID_CLIENT_ID', 'PLAID_ENV',
                'PLAID_SECRET_SANDBOX', 'PLAID_SECRET_PRODUCTION',
                'PLAID_REDIRECT_URI_SANDBOX', 'PLAID_REDIRECT_URI_PRODUCTION')
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $k = $Matches[1]; $v = $Matches[2].Trim().Trim('"').Trim("'")
            if ($wanted -contains $k -and $v) { $fromEnv[$k] = $v }
        }
    }
}

Write-Host ''
Write-Host ($(if ($DryRun) { 'Would set on the linked Railway service:' } else { 'Setting on the linked Railway service:' }))
Write-Host ''

# --- Core --------------------------------------------------------------------
Set-RailwayVar 'APP_ENV'        'production'
Set-RailwayVar 'SECRET_KEY'     $secretKey     -Secret
Set-RailwayVar 'ENCRYPTION_KEY' $encryptionKey -Secret

# --- Storage -----------------------------------------------------------------
# Four slashes: sqlite:// is the scheme, the fourth begins an absolute path.
# Three would make it relative to the working directory and land the database
# inside the container image, where the next deploy discards it.
Set-RailwayVar 'DATABASE_URL'  "sqlite:///$VolumePath/checkbook.db"
Set-RailwayVar 'UPLOAD_FOLDER' "$VolumePath/uploads"

# --- Behaviour ---------------------------------------------------------------
# Safe here only because the Procfile pins gunicorn to one worker; the race
# config.py warns about needs two processes to happen.
Set-RailwayVar 'AUTO_UPGRADE_DB'   '1'
Set-RailwayVar 'SYNC_AUTO_ENABLED' '1'
Set-RailwayVar 'APP_HTTPS'         '1'
# Railway terminates TLS at one edge proxy in front of this container. Left at
# 0, dough/auth.py ignores X-Forwarded-For and every request appears to come
# from the proxy -- so the login throttle would share a single bucket across all
# clients, and audit rows would record the proxy's address as the actor's.
Set-RailwayVar 'TRUSTED_PROXIES'   '1'
Set-RailwayVar 'PUBLIC_BASE_URL'   $PublicBaseUrl

foreach ($k in $fromEnv.Keys | Sort-Object) {
    Set-RailwayVar $k $fromEnv[$k] -Secret
}

Write-Host ''
if (-not $fromEnv.ContainsKey('ANTHROPIC_API_KEY')) {
    Write-Host 'Note: ANTHROPIC_API_KEY was not found in .env. The AI surfaces will report themselves unconfigured; everything else works.'
}
Write-Host 'Done. MAIL_BACKEND is left at "console": password-reset links print to the'
Write-Host 'deploy logs rather than being sent. Set MAIL_BACKEND=smtp and MAIL_SERVER if'
Write-Host 'anyone other than you needs to be able to reset a password.'
Write-Host ''
