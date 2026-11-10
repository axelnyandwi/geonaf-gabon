# GEONAF - shared protections of the backup scripts (D2.9c). Dot-sourced, never run alone.
#
# Rules enforced here (docs/supabase_backup_d2_9c.md):
# - Connection settings travel through libpq environment variables of this process only (PGHOST, PGUSER...),
#   never on a command line, never in a log; the password is typed at run time (Read-Host -AsSecureString),
#   lives in PGPASSWORD for the duration of the run and is removed in a finally block.
# - Hosts: Supabase pooler or direct host for a backup, loopback only for a restore.
# - Output: a new directory outside the Git repository and outside OneDrive, readable by the current user only.
# - Native commands: exit code checked after each call; any failure stops the script.

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$script:GeonafRepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
# Windows or not, read without $IsWindows (absent from Windows PowerShell 5.1 and refused by StrictMode there).
$script:GeonafOnWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
$script:GeonafExpectedTables = @('acquisitions', 'analysis_acquisitions', 'analysis_runs', 'indicator_definitions',
                                 'indicator_results', 'sources', 'territories', 'territory_versions')
$script:GeonafConnectionVariables = @('PGHOST', 'PGHOSTADDR', 'PGPORT', 'PGUSER', 'PGDATABASE', 'PGPASSWORD', 'PGPASSFILE',
                                      'PGSERVICE', 'PGSERVICEFILE', 'PGSSLMODE', 'PGSSLROOTCERT', 'PGOPTIONS', 'PGAPPNAME',
                                      'PGCONNECT_TIMEOUT')

class GeonafRefusal : System.Exception {
    GeonafRefusal([string] $message) : base($message) {}
}

function Stop-Geonaf([string] $Message) {
    throw [GeonafRefusal]::new("GEONAF REFUS : $Message")
}

function Test-GeonafLoopbackHost([string] $HostName) {
    return $HostName -in @('localhost', '127.0.0.1', '::1')
}

function Test-GeonafSupabaseHost([string] $HostName) {
    # Session or transaction pooler (IPv4) and direct host (IPv6 only on the free plan).
    return ($HostName -match '^aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com$') -or
           ($HostName -match '^db\.[a-z0-9]{20}\.supabase\.co$')
}

function Get-GeonafMaskedHost([string] $HostName) {
    if (Test-GeonafLoopbackHost $HostName) { return $HostName }
    if ($HostName -match '\.pooler\.supabase\.com$') { return '<region>.pooler.supabase.com' }
    if ($HostName -match '\.supabase\.co$') { return 'db.<ref>.supabase.co' }
    return '<hote masque>'
}

function Get-GeonafMaskedUser([string] $UserName) {
    if ($UserName -match '^([a-z_]+)\.[a-z0-9]+$') { return "$($Matches[1]).<ref>" }
    return $UserName
}

function Test-GeonafPathUnder([string] $Path, [string] $Root) {
    # Separator of the platform (\ on Windows, / elsewhere): a hard-coded \ never matched a Linux path, so the
    # repository and OneDrive refusals were inoperative there. Case ignored everywhere: refuses more, never less.
    if ([string]::IsNullOrWhiteSpace($Root)) { return $false }
    $separators = [char[]]@('\', '/')
    $sep = [System.IO.Path]::DirectorySeparatorChar
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd($separators) + $sep
    $base = [System.IO.Path]::GetFullPath($Root).TrimEnd($separators) + $sep
    return $full.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-GeonafSafeOutputPath([string] $Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if (Test-GeonafPathUnder $full $script:GeonafRepoRoot) {
        Stop-Geonaf "le dossier de sortie est dans le depot Git ($script:GeonafRepoRoot)."
    }
    foreach ($name in 'OneDrive', 'OneDriveCommercial', 'OneDriveConsumer') {
        if (Test-GeonafPathUnder $full ([Environment]::GetEnvironmentVariable($name))) {
            Stop-Geonaf 'le dossier de sortie est synchronise par OneDrive.'
        }
    }
    if ($full -match '[\\/]OneDrive( - [^\\/]+)?([\\/]|$)') { Stop-Geonaf 'le dossier de sortie est synchronise par OneDrive.' }
    if ($full.StartsWith('\\')) { Stop-Geonaf 'dossier reseau refuse : la sauvegarde reste sur le poste.' }
    return $full
}

function New-GeonafPrivateDirectory([string] $Path) {
    # A new directory only (never reuse one), then inheritance removed and full control for the current user.
    if (Test-Path -LiteralPath $Path) { Stop-Geonaf "le dossier existe deja, il n'est jamais reutilise : $Path" }
    # Private rights are set with icacls only: elsewhere, refuse before creating anything rather than write a
    # backup or a report into a directory readable by others.
    if (-not $script:GeonafOnWindows) { Stop-Geonaf 'execution reservee a Windows : droits prives du dossier non garantis sur ce systeme.' }
    New-Item -ItemType Directory -Path $Path | Out-Null
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Geonaf "droits du dossier non restreints (icacls code $LASTEXITCODE)." }
}

function Find-GeonafPgTool([string] $Name, [string] $PgBin) {
    # pg_dump.exe on Windows, pg_dump elsewhere (Linux and macOS executables carry no extension).
    $file = if ($script:GeonafOnWindows) { "$Name.exe" } else { $Name }
    $candidates = @()
    if ($PgBin) { $candidates += Join-Path $PgBin $file }
    $onPath = Get-Command $file -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { $candidates += $onPath.Source }
    if ($script:GeonafOnWindows) {
        $candidates += Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin' -Filter $file -ErrorAction SilentlyContinue |
                       Sort-Object FullName -Descending | ForEach-Object FullName
    }
    foreach ($candidate in $candidates) { if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate } }
    return $null
}

function Get-GeonafPgToolMajor([string] $Exe) {
    $text = (& $Exe --version) 2>$null
    if ($LASTEXITCODE -ne 0 -or -not ($text -match '\(PostgreSQL\) (\d+)(\.(\d+))?')) {
        Stop-Geonaf "version illisible pour $Exe."
    }
    return [pscustomobject]@{ Major = [int]$Matches[1]; Text = ($text | Select-Object -First 1) }
}

function Assert-GeonafCleanPgEnvironment {
    foreach ($name in $script:GeonafConnectionVariables) {
        if ([Environment]::GetEnvironmentVariable($name)) {
            Stop-Geonaf "la variable $name est deja definie dans cette session : la supprimer avant de relancer."
        }
    }
}

function Set-GeonafConnection([hashtable] $Settings) {
    foreach ($key in $Settings.Keys) { Set-Item -Path "Env:$key" -Value ([string]$Settings[$key]) }
}

function Clear-GeonafConnection {
    foreach ($name in $script:GeonafConnectionVariables) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
}

function Read-GeonafPassword([string] $Prompt) {
    $secure = Read-Host -AsSecureString -Prompt $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr); $secure.Dispose() }
}

function Invoke-GeonafNative {
    # Runs a native tool; stderr goes to a log file in the private directory; stops on a non-zero exit code.
    param([string] $Exe, [string[]] $Arguments, [string] $LogFile, [string] $What, [string] $StdoutFile)
    if ($StdoutFile) {
        & $Exe @Arguments 2>> $LogFile | Out-File -LiteralPath $StdoutFile -Encoding utf8
    } else {
        & $Exe @Arguments 2>> $LogFile | Out-Null
    }
    $code = $LASTEXITCODE
    if ($code -ne 0) { Stop-Geonaf "$What a echoue (code $code). Voir le journal $LogFile." }
}

function Get-GeonafSha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-GeonafStructure([string] $Path) {
    # psql -At -F '|' output of legacy_structure.sql: keeps the result rows, drops the set_config lines.
    $rows = Get-Content -LiteralPath $Path -Encoding utf8 | Where-Object { $_ -match '^(structure|information|empreinte)\|' }
    $digest = $rows | Where-Object { $_ -like 'empreinte|structure_sha256|*' } | Select-Object -First 1
    if (-not $digest) { Stop-Geonaf "empreinte de structure absente de $Path." }
    $parts = $digest.Split('|')
    return [pscustomobject]@{ Sha256 = $parts[3]; Items = [int]$parts[2]; Rows = @($rows) }
}

function Get-GeonafTocSummary([string] $TocFile) {
    # pg_restore --list lines: "<id>; <catalog oid> <object oid> <description> <schema> <name> <owner>".
    $descriptions = 'TABLE DATA', 'FK CONSTRAINT', 'ROW SECURITY', 'SEQUENCE OWNED BY', 'SEQUENCE SET', 'DEFAULT ACL',
                    'TABLE', 'SCHEMA', 'COMMENT', 'ACL', 'CONSTRAINT', 'INDEX', 'SEQUENCE', 'DEFAULT', 'POLICY', 'TRIGGER',
                    'VIEW', 'FUNCTION', 'TYPE', 'DOMAIN', 'EXTENSION', 'MATERIALIZED VIEW'
    $entries = foreach ($line in Get-Content -LiteralPath $TocFile -Encoding utf8) {
        if ($line -match '^\s*;' -or -not ($line -match '^\d+;\s+\d+\s+\d+\s+(.+)$')) { continue }
        $rest = $Matches[1]
        $desc = $descriptions | Where-Object { $rest.StartsWith("$_ ") } | Select-Object -First 1
        if (-not $desc) { [pscustomobject]@{ Kind = 'INCONNU'; Schema = ''; Name = $rest }; continue }
        $tail = $rest.Substring($desc.Length + 1).Split(' ')
        $name = if ($tail.Count -ge 3) { $tail[1..($tail.Count - 2)] -join ' ' } else { ($tail | Select-Object -Skip 1) -join ' ' }
        [pscustomobject]@{ Kind = $desc; Schema = $tail[0]; Name = $name; Owner = $tail[-1] }
    }
    return @($entries)
}
