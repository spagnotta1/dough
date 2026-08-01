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

    # Let strangers create accounts at /register. Off unless asked for, and set
    # explicitly either way rather than left to config.py's default -- running
    # this script should produce a known state, not one that depends on what a
    # previous run happened to leave behind.
    #
    # Each registration creates its own household and owns it, so a new account
    # starts empty and cannot see anybody else's accounts or transactions. What
    # it does mean is that this URL, which fronts real bank data, will accept
    # signups from anyone who finds it.
    [switch]$AllowRegistration,

    # Print what would be set, touching nothing.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

# Windows PowerShell 5.1 encodes anything piped to a native process using
# $OutputEncoding, which defaults to a UTF8Encoding that emits a byte-order
# mark. Piping a secret to `railway variable set --stdin` therefore stores
# EF BB BF followed by the value, and nothing anywhere reports a problem: the
# variable exists, it looks right in the dashboard, and it is three bytes wrong.
#
# For ENCRYPTION_KEY that is the exact failure this script exists to prevent --
# a 45-character Fernet key is not a valid Fernet key, and the deployment finds
# out at the first sync rather than at boot. Worth knowing that reading the
# values back with `railway variable list --kv` does not show it either; only
# --json does.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false

# Normalised so the DATABASE_URL below is built from a known shape. A relative
# mount path would produce a three-slash sqlite:// URL, which SQLAlchemy reads
# as relative to the working directory -- a database inside the image, silently
# discarded by the next deploy.
$VolumePath = '/' + $VolumePath.Trim().Trim('/')

if (-not (Get-Command railway -ErrorAction SilentlyContinue)) {
    throw "The Railway CLI is not on PATH. Install it with 'npm i -g @railway/cli', then run 'railway login' and 'railway link'."
}

function Set-RailwayVar {
    param([string]$Name, [string]$Value, [switch]$Secret)

    $shown = if ($Secret) { '<hidden>' } else { $Value }
    if ($DryRun) {
        Write-Host ("  {0,-22} {1}" -f $Name, $shown)
        return
    }

    # Everything goes in as a KEY=value argument, including the secrets, and the
    # obvious alternative is deliberately not used.
    #
    # `railway variable set --stdin KEY` exists precisely so a secret never
    # appears in a command line, which is what this script would want. On CLI
    # 5.30.1 it corrupts the value: it stores EF BB BF in front of whatever it
    # is given. That was measured against a process fed exactly six bytes with
    # no byte-order mark, and nine came back -- so the CLI adds it, and no
    # amount of encoding care on this side prevents it. Every variable set that
    # way was three bytes wrong, silently, including the Fernet key.
    #
    # A corrupt ENCRYPTION_KEY is a far worse outcome than a value briefly
    # visible in the local process table, so the argument form wins until the
    # CLI is fixed. Note that the values do not reach PowerShell's history:
    # history records the line the user typed -- the call to this script -- not
    # the arguments this script constructs for its children.
    #
    # --skip-deploys because these are set one at a time: without it each of the
    # eighteen triggers its own redeploy, and the first seventeen boot against
    # an incomplete configuration. The caller deploys once, afterwards.
    & railway variable set "$Name=$Value" --skip-deploys | Out-Null
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
    # The MAIL_* names are here for a reason worth stating: without them this
    # script pushed no mail configuration at all, so every deployment ran on
    # config.py's default of MAIL_BACKEND=console however carefully .env was
    # filled in. The symptom is not an error anywhere -- verification and
    # password-reset mail is "sent" successfully, into the deploy log, and the
    # person waiting on it simply never receives anything.
    $wanted = @('ANTHROPIC_API_KEY', 'PLAID_CLIENT_ID', 'PLAID_ENV',
                'PLAID_SECRET_SANDBOX', 'PLAID_SECRET_PRODUCTION',
                'PLAID_REDIRECT_URI_SANDBOX', 'PLAID_REDIRECT_URI_PRODUCTION',
                'MAIL_BACKEND', 'MAIL_FROM', 'MAIL_DEFAULT_SENDER',
                'MAIL_SERVER', 'MAIL_PORT', 'MAIL_USERNAME', 'MAIL_PASSWORD',
                'MAIL_USE_TLS')
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

# --- Serving -----------------------------------------------------------------
# Railway injects a PORT of its own choosing if this is unset, and the generated
# domain then has to guess which port to route to. Pinning both sides to the
# same number makes the routing a fact rather than a detection: the Procfile
# binds $PORT, and the domain is created with --port 8080 to match.
Set-RailwayVar 'PORT' '8080'

# --- Behaviour ---------------------------------------------------------------
# AUTO_UPGRADE_DB is deliberately not set. `ProductionConfig` assigns it False
# as a class attribute (config.py), so under APP_ENV=production the environment
# variable is read and then overwritten -- setting it to 1 looks like it enables
# boot-time migrations and does nothing at all. That is by design: migrations
# are a deploy step, not a side effect of a process starting. Run the chain
# yourself after a schema change; see docs/deploy-railway.md.
Set-RailwayVar 'SYNC_AUTO_ENABLED'   '1'
Set-RailwayVar 'APP_HTTPS'           '1'
Set-RailwayVar 'ALLOW_REGISTRATION'  $(if ($AllowRegistration) { '1' } else { '0' })
# Railway terminates TLS at one edge proxy in front of this container. Left at
# 0, dough/auth.py ignores X-Forwarded-For and every request appears to come
# from the proxy -- so the login throttle would share a single bucket across all
# clients, and audit rows would record the proxy's address as the actor's.
Set-RailwayVar 'TRUSTED_PROXIES'     '1'
Set-RailwayVar 'PUBLIC_BASE_URL'     $PublicBaseUrl

foreach ($k in $fromEnv.Keys | Sort-Object) {
    Set-RailwayVar $k $fromEnv[$k] -Secret
}

# --- Verification ------------------------------------------------------------
# Read back and prove it, rather than trusting that eighteen successful exit
# codes mean eighteen correct values. The first version of this script set every
# secret with a BOM in front of it and reported complete success.
#
# Two traps are deliberately avoided here. `railway variable list --kv` does not
# reveal the corruption, only --json does. And `-eq`/`-ceq` compare
# culture-sensitively, which treats U+FEFF as a zero-weight character and
# happily calls a BOM-prefixed key equal to the real one -- the comparison has
# to be Ordinal.
if (-not $DryRun) {
    Write-Host ''
    Write-Host 'Verifying:'
    $stored = railway variable list --json | Out-String | ConvertFrom-Json

    $corrupt = @()
    foreach ($prop in $stored.PSObject.Properties) {
        $b = [Text.Encoding]::UTF8.GetBytes([string]$prop.Value)
        if ($b.Length -ge 3 -and $b[0] -eq 0xef -and $b[1] -eq 0xbb -and $b[2] -eq 0xbf) {
            $corrupt += $prop.Name
        }
    }
    if ($corrupt.Count) {
        throw ("These variables were stored with a byte-order mark and are wrong: {0}. " -f ($corrupt -join ', ')) +
              'Do not deploy until this is resolved.'
    }
    Write-Host '  no byte-order marks           ok'

    if (-not [string]::Equals($stored.ENCRYPTION_KEY, $encryptionKey, [StringComparison]::Ordinal)) {
        throw 'ENCRYPTION_KEY as stored does not byte-for-byte match .sync_encryption_key. ' +
              'Deploying now would make every stored institution token unreadable.'
    }
    Write-Host '  ENCRYPTION_KEY byte-exact     ok'

    foreach ($required in @('APP_ENV', 'SECRET_KEY', 'DATABASE_URL', 'PUBLIC_BASE_URL')) {
        if (-not $stored.$required) { throw "$required is not set on the service." }
    }
    Write-Host '  required variables present    ok'
}

Write-Host ''
if (-not $fromEnv.ContainsKey('ANTHROPIC_API_KEY')) {
    Write-Host 'Note: ANTHROPIC_API_KEY was not found in .env. The AI surfaces will report themselves unconfigured; everything else works.'
}
if ($AllowRegistration) {
    Write-Host 'Registration is OPEN: anyone who reaches /register can create an account.'
    Write-Host 'Each one gets its own empty household and cannot see yours.'
    Write-Host ''
}

# Reported from what was actually pushed rather than assumed. The previous
# version of this message said "MAIL_BACKEND is left at console" unconditionally,
# which was true only because the script had no way to set it -- and it stayed on
# screen, reassuringly specific, while being the thing nobody acted on.
$mailBackend = $fromEnv['MAIL_BACKEND']
if ($mailBackend -eq 'smtp') {
    if (-not $fromEnv.ContainsKey('MAIL_SERVER')) {
        Write-Host 'WARNING: MAIL_BACKEND=smtp was pushed but MAIL_SERVER was not found in .env.'
        Write-Host 'The application refuses to build that backend, so no mail will be sent and'
        Write-Host 'every send will report a delivery failure. Add MAIL_SERVER and re-run.'
    } elseif (-not ($fromEnv.ContainsKey('MAIL_FROM') -or $fromEnv.ContainsKey('MAIL_DEFAULT_SENDER'))) {
        # Worth its own branch rather than folding into the MAIL_SERVER warning,
        # because this one still boots, still connects, and still authenticates.
        # config.py falls back to dough@localhost, which a hosted relay refuses
        # per message -- so the symptom is every send failing at the last step
        # with the transport apparently working perfectly.
        Write-Host 'WARNING: MAIL_BACKEND=smtp was pushed but no MAIL_FROM was found in .env.'
        Write-Host 'The From address falls back to dough@localhost, which a hosted provider'
        Write-Host 'rejects for every message because it is not a verified sender. Set MAIL_FROM'
        Write-Host 'to an address confirmed with your provider and re-run.'
    } else {
        Write-Host ("Mail goes out over SMTP via {0}. Confirm it by changing your address on" -f $fromEnv['MAIL_SERVER'])
        Write-Host '/settings and checking the new inbox for the confirmation link.'
    }
} else {
    Write-Host 'MAIL_BACKEND is "console": verification and password-reset links print to the'
    Write-Host 'deploy logs instead of being sent, so nothing arrives in anybody''s inbox and'
    Write-Host 'nobody locked out can reach a reset link. Put MAIL_BACKEND=smtp, MAIL_SERVER,'
    Write-Host 'MAIL_USERNAME, MAIL_PASSWORD and MAIL_FROM in .env and re-run this script.'
    if ($AllowRegistration) {
        Write-Host 'With registration open this is a real gap rather than a cosmetic one: a'
        Write-Host 'registrant who forgets a password has no way back in, and you cannot give'
        Write-Host 'them one without reading the logs. Do it before pointing anybody at this URL.'
    }
}
Write-Host ''
