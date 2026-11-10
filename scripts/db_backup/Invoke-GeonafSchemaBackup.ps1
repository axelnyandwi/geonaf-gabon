<#
.SYNOPSIS
GEONAF - logical backup of the legacy geonaf schema with pg_dump (D2.9c). Dry run unless -Execute.

.DESCRIPTION
Without -Execute: validates the parameters, the tools and the output directory, prints the plan with the host
and user masked, opens no connection and writes nothing.
With -Execute (only after an explicit authorization): asks the password (never echoed, never stored), then
1. reads the server version, the instance start and the table count (one read-only transaction);
2. pg_dump --format=custom --schema=geonaf (pg_dump runs in a read-only REPEATABLE READ transaction);
3. records the OID-free structure (legacy_structure.sql), the 09 fingerprint and the row counts, read only;
4. offline: table of contents and schema SQL from the archive, SHA-256 of every file, manifest.json;
5. runs Test-GeonafBackupArchive.ps1 on the result.
Writes only into a new directory <OutputRoot>\<label>\<UTC time>, outside the repository and OneDrive, readable
by the current user only. Backs up the geonaf schema only: not the Supabase project (see the documentation).

.EXAMPLE
pwsh -File scripts/db_backup/Invoke-GeonafSchemaBackup.ps1 -ProjectLabel geonaf-test `
     -PgHost aws-0-<region>.pooler.supabase.com -PgUser postgres.<ref> -PgBin 'C:\Program Files\PostgreSQL\17\bin'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateSet('geonaf-test', 'geonaf-dev', 'local-test')] [string] $ProjectLabel,
    [Parameter(Mandatory)] [string] $PgHost,
    [Parameter(Mandatory)] [string] $PgUser,
    [int] $PgPort = 5432,
    [string] $PgDatabase = 'postgres',
    [string] $PgBin,
    # Profile from the OS (USERPROFILE on Windows, HOME elsewhere): the USERPROFILE variable does not exist on Linux.
    [string] $OutputRoot = (Join-Path (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.geonaf') 'backups'),
    [string] $SslRootCert,
    [switch] $NoPasswordPrompt,
    [switch] $Execute
)
. (Join-Path $PSScriptRoot 'GeonafBackup.Common.ps1')

try {
    # ---- Checks that need no connection -------------------------------------------------------------
    $local = Test-GeonafLoopbackHost $PgHost
    if ($ProjectLabel -eq 'local-test' -and -not $local) { Stop-Geonaf 'local-test exige un hote local (127.0.0.1, localhost, ::1).' }
    if ($ProjectLabel -ne 'local-test') {
        if (-not (Test-GeonafSupabaseHost $PgHost)) { Stop-Geonaf 'hote non reconnu : pooler Supabase ou hote direct db.<ref>.supabase.co seulement.' }
        if ($PgPort -ne 5432) { Stop-Geonaf 'port 5432 seulement (pooler en mode session ou connexion directe) ; le mode transaction (6543) ne convient pas a pg_dump.' }
        if ($NoPasswordPrompt) { Stop-Geonaf '-NoPasswordPrompt est reserve a un serveur local de test.' }
        if ($SslRootCert -and -not (Test-Path -LiteralPath $SslRootCert)) { Stop-Geonaf 'certificat racine introuvable (-SslRootCert).' }
    }
    if ($PgDatabase -notmatch '^[a-z_][a-z0-9_]{0,62}$') { Stop-Geonaf 'nom de base invalide.' }
    $tools = @{}
    foreach ($tool in 'pg_dump', 'pg_restore', 'psql') {
        $exe = Find-GeonafPgTool $tool $PgBin
        if (-not $exe) { Stop-Geonaf "$tool introuvable (utiliser -PgBin)." }
        $tools[$tool] = [pscustomobject]@{ Exe = $exe; Version = Get-GeonafPgToolMajor $exe }
    }
    $root = Assert-GeonafSafeOutputPath $OutputRoot
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $target = Join-Path (Join-Path $root $ProjectLabel) $stamp
    $sslMode = if ($local) { 'prefer' } elseif ($SslRootCert) { 'verify-full' } else { 'require' }

    Write-Host "GEONAF sauvegarde du schema geonaf - $ProjectLabel"
    Write-Host ("  hote {0}:{1}, utilisateur {2}, base {3}, sslmode {4}" -f (Get-GeonafMaskedHost $PgHost), $PgPort,
                (Get-GeonafMaskedUser $PgUser), $PgDatabase, $sslMode)
    Write-Host "  pg_dump : $($tools['pg_dump'].Version.Text)"
    Write-Host "  dossier : $target"
    if ($sslMode -eq 'require') { Write-Host '  ATTENTION : chiffrement sans verification du certificat du serveur (fournir -SslRootCert).' }
    if (-not $Execute) {
        Write-Host 'SIMULATION : aucune connexion, aucun fichier ecrit. Relancer avec -Execute apres autorisation.'
        exit 0
    }

    # ---- Execution ------------------------------------------------------------------------------------
    Assert-GeonafCleanPgEnvironment
    $settings = @{ PGHOST = $PgHost; PGPORT = $PgPort; PGUSER = $PgUser; PGDATABASE = $PgDatabase; PGSSLMODE = $sslMode
                   PGAPPNAME = 'geonaf-backup-d2.9c'; PGCONNECT_TIMEOUT = 10 }
    if ($sslMode -eq 'verify-full') { $settings.PGSSLROOTCERT = (Resolve-Path -LiteralPath $SslRootCert).Path }
    Set-GeonafConnection $settings
    if (-not $NoPasswordPrompt) { Read-GeonafPassword "Mot de passe de $(Get-GeonafMaskedUser $PgUser) (non affiche)" }
    New-GeonafPrivateDirectory $target
    $log = Join-Path $target 'backup.log'
    $psqlBase = @('-X', '-q', '-1', '-At', '-F', '|', '-v', 'ON_ERROR_STOP=1')

    # 1. Server, instance and table count, read only.
    $serverSql = Join-Path $target 'server.sql'
    @("SELECT set_config('transaction_read_only', 'on', true);",
      "SELECT 'serveur', current_setting('server_version_num'), current_setting('server_version'),",
      "       to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US'),",
      "       (to_regnamespace('geonaf') IS NOT NULL)::text,",
      "       (SELECT count(*) FROM pg_class WHERE relnamespace = to_regnamespace('geonaf') AND relkind IN ('r', 'p'));") |
        Set-Content -LiteralPath $serverSql -Encoding utf8
    $serverOut = Join-Path $target 'server.txt'
    Invoke-GeonafNative $tools['psql'].Exe ($psqlBase + @('-f', $serverSql)) $log 'lecture de la version du serveur' $serverOut
    $server = (Get-Content -LiteralPath $serverOut | Where-Object { $_ -like 'serveur|*' } | Select-Object -First 1).Split('|')
    $serverMajor = [int]([int]$server[1] / 10000)
    if ($tools['pg_dump'].Version.Major -lt $serverMajor) { Stop-Geonaf "pg_dump $($tools['pg_dump'].Version.Major) trop ancien pour le serveur $serverMajor." }
    if ($server[4] -ne 'true' -or [int]$server[5] -ne 8) { Stop-Geonaf "schema geonaf absent ou nombre de tables different de 8 ($($server[5]))." }

    # 2. The archive. Read-only transaction by pg_dump itself; waits at most 10 s for a lock.
    $archive = Join-Path $target 'geonaf.dump'
    $dumpOptions = @('--format=custom', '--schema=geonaf', '--lock-wait-timeout=10s', '--verbose', "--file=$archive")
    Invoke-GeonafNative $tools['pg_dump'].Exe $dumpOptions $log 'pg_dump'

    # 3. Structure, fingerprint and row counts, read only, at the same time as the archive.
    $structure = Join-Path $target 'structure.txt'
    Invoke-GeonafNative $tools['psql'].Exe ($psqlBase + @('-f', (Join-Path $PSScriptRoot 'legacy_structure.sql'))) $log 'structure' $structure
    $fingerprint = Join-Path $target 'fingerprint.txt'
    $fingerprintSql = Join-Path $script:GeonafRepoRoot 'db\supabase\09_legacy_schema_fingerprint.sql'
    Invoke-GeonafNative $tools['psql'].Exe ($psqlBase + @('-f', $fingerprintSql)) $log 'empreinte 09' $fingerprint
    $countSql = Join-Path $target 'rowcounts.sql'
    $selects = $script:GeonafExpectedTables | ForEach-Object { "SELECT 'lignes', '$_', count(*) FROM geonaf.`"$_`"" }
    @("SELECT set_config('transaction_read_only', 'on', true);", (($selects -join "`nUNION ALL ") + ';')) |
        Set-Content -LiteralPath $countSql -Encoding utf8
    $counts = Join-Path $target 'rowcounts.txt'
    Invoke-GeonafNative $tools['psql'].Exe ($psqlBase + @('-f', $countSql)) $log 'comptage des lignes' $counts
} catch [GeonafRefusal] {
    Write-Host $_.Exception.Message
    exit 2
} finally {
    Clear-GeonafConnection     # Password and connection settings never outlive the run.
}

try {
    # 4. Offline: table of contents, schema SQL, digests and manifest (no connection from here on).
    $toc = Join-Path $target 'geonaf.toc.txt'
    Invoke-GeonafNative $tools['pg_restore'].Exe @('--list', $archive) $log 'pg_restore --list' $toc
    $schemaSql = Join-Path $target 'geonaf.schema.sql'
    Invoke-GeonafNative $tools['pg_restore'].Exe @('--schema-only', "--file=$schemaSql", $archive) $log 'export SQL du schema'
    $entries = Get-GeonafTocSummary $toc
    $structureInfo = Read-GeonafStructure $structure
    $fpLine = (Get-Content -LiteralPath $fingerprint | Where-Object { $_ -match '^\d{4}-' } | Select-Object -Last 1).Split('|')
    $rowCounts = [ordered]@{}
    Get-Content -LiteralPath $counts | Where-Object { $_ -like 'lignes|*' } | ForEach-Object { $p = $_.Split('|'); $rowCounts[$p[1]] = [int64]$p[2] }
    $files = [ordered]@{}
    foreach ($name in 'geonaf.dump', 'geonaf.toc.txt', 'geonaf.schema.sql', 'structure.txt', 'fingerprint.txt', 'rowcounts.txt', 'server.txt') {
        $files[$name] = Get-GeonafSha256 (Join-Path $target $name)
    }
    $manifest = [ordered]@{
        format = 'geonaf-schema-backup/1'
        portee = 'schema geonaf seulement ; pas les roles, extensions, autres schemas, Auth, Storage ni parametres du projet'
        date_utc = [DateTime]::UtcNow.ToString('o'); project_label = $ProjectLabel
        hote = Get-GeonafMaskedHost $PgHost; utilisateur = Get-GeonafMaskedUser $PgUser; base = $PgDatabase
        serveur = [ordered]@{ version = $server[2]; version_num = [int]$server[1]; demarrage_instance_utc = $server[3] }
        outils = [ordered]@{ pg_dump = $tools['pg_dump'].Version.Text; pg_restore = $tools['pg_restore'].Version.Text; psql = $tools['psql'].Version.Text }
        pg_dump_options = @($dumpOptions | Where-Object { $_ -notlike '--file=*' })
        archive = [ordered]@{ fichier = 'geonaf.dump'; sha256 = $files['geonaf.dump']; octets = (Get-Item -LiteralPath $archive).Length }
        sommaire = [ordered]@{}
        tables = @($entries | Where-Object Kind -eq 'TABLE' | ForEach-Object Name | Sort-Object)
        lignes = $rowCounts
        structure = [ordered]@{ sha256 = $structureInfo.Sha256; elements = $structureInfo.Items }
        empreinte_09 = [ordered]@{ instance_utc = $fpLine[1]; oid_geonaf = $fpLine[3]; oid_geonaf_legacy_draft = $fpLine[4]
                                   commentaire_geonaf_json = $fpLine[5]; tables = [int]$fpLine[7]; sha256 = $fpLine[8] }
        fichiers_sha256 = $files
    }
    $entries | Group-Object Kind | Sort-Object Name | ForEach-Object { $manifest.sommaire[$_.Name] = $_.Count }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $target 'manifest.json') -Encoding utf8
    $files.GetEnumerator() | ForEach-Object { "$($_.Value)  $($_.Key)" } | Set-Content -LiteralPath (Join-Path $target 'SHA256SUMS') -Encoding ascii
    Remove-Item -LiteralPath $serverSql, $countSql

    # 5. Independent check of the result.
    & (Join-Path $PSScriptRoot 'Test-GeonafBackupArchive.ps1') -BackupDir $target -PgBin (Split-Path $tools['pg_restore'].Exe)
    if ($LASTEXITCODE -ne 0) { Stop-Geonaf 'la verification de l''archive a echoue.' }
    Write-Host "SAUVEGARDE TERMINEE ET VERIFIEE : $target"
    Write-Host "  archive SHA-256 $($files['geonaf.dump'])"
    Write-Host "  structure SHA-256 $($structureInfo.Sha256) ; empreinte 09 $($fpLine[8]) (OID $($fpLine[3]))"
    exit 0
} catch [GeonafRefusal] {
    Write-Host $_.Exception.Message
    exit 2
}
