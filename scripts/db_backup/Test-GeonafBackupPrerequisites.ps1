<#
.SYNOPSIS
GEONAF - prerequisites of the legacy schema backup (D2.9c). Read only; no connection unless -TestNetwork.

.DESCRIPTION
Checks the PostgreSQL client tools and their major version against the server (17 for Supabase 17.11), the
output directory (outside the repository and OneDrive), the PG* variables of the session and, only with
-TestNetwork, that a TCP connection to the given host and port can be opened (no PostgreSQL login, no query).
-TestNetwork contacts the Supabase infrastructure: run it only after an explicit authorization.
Exit code 0 when every check passes, 1 otherwise.

.EXAMPLE
pwsh -File scripts/db_backup/Test-GeonafBackupPrerequisites.ps1 -PgBin 'C:\Program Files\PostgreSQL\17\bin'
#>
[CmdletBinding()]
param(
    [string] $PgBin,
    [int] $ServerMajor = 17,
    [string] $OutputRoot = (Join-Path $env:USERPROFILE '.geonaf\backups'),
    [switch] $TestNetwork,
    [string] $PgHost,
    [int] $PgPort = 5432
)
. (Join-Path $PSScriptRoot 'GeonafBackup.Common.ps1')

$results = [System.Collections.Generic.List[object]]::new()
function Add-Check([string] $Check, [bool] $Ok, [string] $Detail) {
    $results.Add([pscustomobject]@{ Controle = $Check; Conforme = $Ok; Detail = $Detail })
}

Add-Check 'PowerShell 7 ou plus' ($PSVersionTable.PSVersion.Major -ge 7) $PSVersionTable.PSVersion.ToString()
foreach ($tool in 'pg_dump', 'pg_restore', 'psql') {
    $exe = Find-GeonafPgTool $tool $PgBin
    if (-not $exe) { Add-Check "$tool present" $false 'absent (PATH, -PgBin, C:\Program Files\PostgreSQL)'; continue }
    $version = Get-GeonafPgToolMajor $exe
    Add-Check "$tool version majeure >= $ServerMajor" ($version.Major -ge $ServerMajor) "$($version.Text) ($exe)"
}
try {
    $full = Assert-GeonafSafeOutputPath $OutputRoot
    Add-Check 'dossier de sortie hors depot et hors OneDrive' $true $full
} catch [GeonafRefusal] { Add-Check 'dossier de sortie hors depot et hors OneDrive' $false $_.Exception.Message }
try { Assert-GeonafCleanPgEnvironment; Add-Check 'aucune variable PG* deja definie' $true '' }
catch [GeonafRefusal] { Add-Check 'aucune variable PG* deja definie' $false $_.Exception.Message }
Add-Check 'icacls disponible (droits du dossier)' ([bool](Get-Command icacls.exe -ErrorAction SilentlyContinue)) ''

if ($TestNetwork) {
    if (-not (Test-GeonafSupabaseHost $PgHost) -and -not (Test-GeonafLoopbackHost $PgHost)) {
        Add-Check 'hote autorise' $false 'hote ni Supabase (pooler ou direct) ni local'
    } else {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $ok = $client.ConnectAsync($PgHost, $PgPort).Wait(5000) -and $client.Connected
            Add-Check "port TCP $PgPort joignable (sans authentification)" $ok (Get-GeonafMaskedHost $PgHost)
        } catch { Add-Check "port TCP $PgPort joignable (sans authentification)" $false 'connexion impossible' }
        finally { $client.Dispose() }
    }
} else {
    Add-Check 'reseau non teste (-TestNetwork absent, aucune connexion)' $true 'ports 5432/6543 fermes depuis ce poste au 2026-11-04'
}

$results | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Write-Host
if ($results.Conforme -contains $false) { Write-Host 'PREREQUIS NON SATISFAITS'; exit 1 }
Write-Host 'PREREQUIS SATISFAITS'
exit 0
