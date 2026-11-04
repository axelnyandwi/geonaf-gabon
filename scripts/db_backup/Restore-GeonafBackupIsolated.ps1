<#
.SYNOPSIS
GEONAF - restore a geonaf schema backup into a NEW local throwaway database (D2.9c). Dry run unless -Execute.

.DESCRIPTION
Never restores into Supabase nor into an existing database:
- the target host must be the loopback (127.0.0.1, localhost, ::1);
- the target database must be named geonaf_restore_<suffix> and must not exist: this script creates it;
- nothing is ever dropped (pg_restore never cleans the target; one transaction, stopping at the first error).
The archive is checked first (Test-GeonafBackupArchive.ps1). After the restore, the OID-free structure
(legacy_structure.sql) and the row counts are compared with the manifest. Owners and grants are not restored
(--no-owner --no-acl: the Supabase roles do not exist locally) and OIDs differ by nature: the 09 fingerprint
cannot match, the structure fingerprint must. The report goes to a new private directory, never into the backup.

.EXAMPLE
pwsh -File scripts/db_backup/Restore-GeonafBackupIsolated.ps1 -BackupDir <dir> -TargetDatabase geonaf_restore_20261105 `
     -TargetPort 5433 -PgBin 'C:\Program Files\PostgreSQL\17\bin' -Execute
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BackupDir,
    [Parameter(Mandatory)] [string] $TargetDatabase,
    [string] $TargetHost = '127.0.0.1',
    [int] $TargetPort = 5432,
    [string] $TargetUser = 'postgres',
    [string] $PgBin,
    [string] $OutputRoot = (Join-Path $env:USERPROFILE '.geonaf\backups'),
    [switch] $NoPasswordPrompt,
    [switch] $Execute
)
. (Join-Path $PSScriptRoot 'GeonafBackup.Common.ps1')

try {
    if (-not (Test-GeonafLoopbackHost $TargetHost)) { Stop-Geonaf 'restauration refusee hors du poste : hote local seulement (jamais geonaf-test ni geonaf-dev).' }
    if ($TargetDatabase -notmatch '^geonaf_restore_[a-z0-9_]{1,40}$') { Stop-Geonaf 'la base cible doit s''appeler geonaf_restore_<suffixe> (minuscules, chiffres, _).' }
    if ($TargetUser -notmatch '^[a-z_][a-z0-9_]{0,62}$') { Stop-Geonaf 'nom de role invalide.' }
    $dir = Assert-GeonafSafeOutputPath $BackupDir
    $manifest = Get-Content -LiteralPath (Join-Path $dir 'manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
    $tools = @{}
    foreach ($tool in 'pg_restore', 'psql') {
        $exe = Find-GeonafPgTool $tool $PgBin
        if (-not $exe) { Stop-Geonaf "$tool introuvable (utiliser -PgBin)." }
        $tools[$tool] = [pscustomobject]@{ Exe = $exe; Version = Get-GeonafPgToolMajor $exe }
    }
    $sourceMajor = [int]([int]$manifest.serveur.version_num / 10000)
    if ($tools['pg_restore'].Version.Major -lt $sourceMajor) { Stop-Geonaf "pg_restore $($tools['pg_restore'].Version.Major) ne lit pas une archive de PostgreSQL $sourceMajor." }

    & (Join-Path $PSScriptRoot 'Test-GeonafBackupArchive.ps1') -BackupDir $dir -PgBin (Split-Path $tools['pg_restore'].Exe)
    if ($LASTEXITCODE -ne 0) { Stop-Geonaf 'archive non conforme : aucune restauration.' }
    # Objects typed with an extension (PostGIS in the Supabase "extensions" schema) need that extension locally.
    $schemaSql = Get-Content -LiteralPath (Join-Path $dir 'geonaf.schema.sql') -Raw -Encoding utf8
    if ($schemaSql -match '\bextensions\.') {
        Stop-Geonaf 'le schema reference le schema extensions (PostGIS ?) : installer l''extension dans un schema extensions de la base locale, puis adapter la procedure (non automatise).'
    }

    $report = Join-Path (Join-Path (Assert-GeonafSafeOutputPath $OutputRoot) 'restores') ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
    Write-Host "GEONAF restauration isolee de $dir"
    Write-Host "  cible ${TargetHost}:$TargetPort, base $TargetDatabase (creee, jamais remplacee), rapport $report"
    if (-not $Execute) {
        Write-Host 'SIMULATION : aucune connexion, aucune base creee. Relancer avec -Execute.'
        exit 0
    }

    Assert-GeonafCleanPgEnvironment
    Set-GeonafConnection @{ PGHOST = $TargetHost; PGPORT = $TargetPort; PGUSER = $TargetUser; PGDATABASE = 'postgres'
                            PGAPPNAME = 'geonaf-restore-d2.9c'; PGCONNECT_TIMEOUT = 10; PGSSLMODE = 'prefer' }
    if (-not $NoPasswordPrompt) { Read-GeonafPassword "Mot de passe local de $TargetUser (non affiche)" }
    New-GeonafPrivateDirectory $report
    $log = Join-Path $report 'restore.log'
    $psqlBase = @('-X', '-q', '-At', '-F', '|', '-v', 'ON_ERROR_STOP=1')

    $exists = & $tools['psql'].Exe @psqlBase -c "SELECT count(*) FROM pg_database WHERE datname = '$TargetDatabase'" 2>> $log
    if ($LASTEXITCODE -ne 0) { Stop-Geonaf "connexion au serveur local impossible (voir $log)." }
    if ([int]$exists -ne 0) { Stop-Geonaf "la base $TargetDatabase existe deja : restauration refusee (jamais sur une base existante)." }
    Invoke-GeonafNative $tools['psql'].Exe ($psqlBase + @('-c', "CREATE DATABASE $TargetDatabase")) $log 'creation de la base locale'
    $env:PGDATABASE = $TargetDatabase
    Invoke-GeonafNative $tools['pg_restore'].Exe @('--no-owner', '--no-acl', '--exit-on-error', '--single-transaction',
        "--dbname=$TargetDatabase", (Join-Path $dir 'geonaf.dump')) $log 'pg_restore'

    $structure = Join-Path $report 'structure.txt'
    Invoke-GeonafNative $tools['psql'].Exe ($psqlBase + @('-1', '-f', (Join-Path $PSScriptRoot 'legacy_structure.sql'))) $log 'structure restauree' $structure
    $restored = Read-GeonafStructure $structure
    $counts = Join-Path $report 'rowcounts.txt'
    $selects = $script:GeonafExpectedTables | ForEach-Object { "SELECT 'lignes', '$_', count(*) FROM geonaf.`"$_`"" }
    Invoke-GeonafNative $tools['psql'].Exe ($psqlBase + @('-1', '-c', ($selects -join ' UNION ALL '))) $log 'comptage' $counts
    $sameCounts = $true
    Get-Content -LiteralPath $counts | Where-Object { $_ -like 'lignes|*' } | ForEach-Object {
        $p = $_.Split('|'); if ([int64]$manifest.lignes.($p[1]) -ne [int64]$p[2]) { $sameCounts = $false }
    }
    $sameStructure = $restored.Sha256 -eq $manifest.structure.sha256
    Write-Host ("  structure : {0} (restauree {1}, sauvegardee {2})" -f $(if ($sameStructure) { 'identique' } else { 'DIFFERENTE' }), $restored.Sha256, $manifest.structure.sha256)
    Write-Host ("  lignes : {0}" -f $(if ($sameCounts) { 'identiques' } else { 'DIFFERENTES' }))
    Write-Host '  non restaures par conception : proprietaires, droits (--no-owner --no-acl), OID (empreinte 09 differente).'
    if (-not ($sameStructure -and $sameCounts)) { Write-Host 'RESTAURATION NON CONFORME'; exit 1 }
    Write-Host "RESTAURATION ISOLEE CONFORME dans $TargetDatabase (a supprimer manuellement apres examen)."
    exit 0
} catch [GeonafRefusal] {
    Write-Host $_.Exception.Message
    exit 2
} finally {
    Clear-GeonafConnection
}
