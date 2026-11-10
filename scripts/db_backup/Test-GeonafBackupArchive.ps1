<#
.SYNOPSIS
GEONAF - offline check of a geonaf schema backup (D2.9c). No connection; writes nothing.

.DESCRIPTION
Checks that the backup directory is outside the repository and OneDrive, that every file matches SHA256SUMS
and manifest.json, that pg_restore can read the archive, and that its table of contents holds exactly the
8 legacy tables of the geonaf schema with their data and nothing from another schema.
Exit code 0 when every check passes, 1 otherwise.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BackupDir,
    [string] $PgBin
)
. (Join-Path $PSScriptRoot 'GeonafBackup.Common.ps1')

$failures = [System.Collections.Generic.List[string]]::new()
function Assert-Check([bool] $Ok, [string] $Label) {
    Write-Host ("  [{0}] {1}" -f $(if ($Ok) { 'conforme' } else { 'ECHEC' }), $Label)
    if (-not $Ok) { $failures.Add($Label) }
}

try {
    $dir = Assert-GeonafSafeOutputPath $BackupDir
    Write-Host "GEONAF verification de la sauvegarde $dir"
    $manifestPath = Join-Path $dir 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { Stop-Geonaf 'manifest.json absent.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    Assert-Check ($manifest.format -eq 'geonaf-schema-backup/1') 'format du manifeste'

    # Digests: SHA256SUMS, manifest and files on disk must agree.
    $sums = @{}
    Get-Content -LiteralPath (Join-Path $dir 'SHA256SUMS') | ForEach-Object {
        if ($_ -match '^([0-9a-f]{64})  (.+)$') { $sums[$Matches[2]] = $Matches[1] }
    }
    foreach ($property in $manifest.fichiers_sha256.PSObject.Properties) {
        $path = Join-Path $dir $property.Name
        $actual = if (Test-Path -LiteralPath $path) { Get-GeonafSha256 $path } else { 'absent' }
        Assert-Check (($actual -eq $property.Value) -and ($sums[$property.Name] -eq $property.Value)) "SHA-256 de $($property.Name)"
    }
    Assert-Check ($manifest.archive.sha256 -eq $manifest.fichiers_sha256.'geonaf.dump') 'SHA-256 de l''archive dans le manifeste'

    # The archive is readable and its table of contents is the expected one.
    $pgRestore = Find-GeonafPgTool 'pg_restore' $PgBin
    if (-not $pgRestore) { Stop-Geonaf 'pg_restore introuvable (utiliser -PgBin).' }
    $listing = & $pgRestore --list (Join-Path $dir 'geonaf.dump') 2>$null
    Assert-Check ($LASTEXITCODE -eq 0) 'archive lisible par pg_restore'
    $tocCopy = [System.IO.Path]::GetTempFileName()
    try {
        $listing | Set-Content -LiteralPath $tocCopy -Encoding utf8
        $entries = Get-GeonafTocSummary $tocCopy
    } finally { Remove-Item -LiteralPath $tocCopy -ErrorAction SilentlyContinue }
    $tables = @($entries | Where-Object Kind -eq 'TABLE' | ForEach-Object Name | Sort-Object)
    $data = @($entries | Where-Object Kind -eq 'TABLE DATA' | ForEach-Object Name | Sort-Object)
    Assert-Check (($tables -join ',') -eq ($script:GeonafExpectedTables -join ',')) "8 tables attendues ($($tables -join ', '))"
    Assert-Check (($data -join ',') -eq ($script:GeonafExpectedTables -join ',')) 'donnees des 8 tables presentes dans l''archive'
    $foreign = @($entries | Where-Object { $_.Schema -notin @('geonaf', '-') })
    Assert-Check ($foreign.Count -eq 0) "aucun objet d'un autre schema ($($foreign.Count))"
    Assert-Check (@($entries | Where-Object Kind -eq 'INCONNU').Count -eq 0) 'aucune entree de sommaire inconnue'
    Assert-Check (@($entries | Where-Object { $_.Kind -eq 'SCHEMA' -and $_.Name -eq 'geonaf' }).Count -eq 1) 'schema geonaf present'
    $manifestCounts = @{}
    $manifest.sommaire.PSObject.Properties | ForEach-Object { $manifestCounts[$_.Name] = [int]$_.Value }
    $same = $true
    $entries | Group-Object Kind | ForEach-Object { if ($manifestCounts[$_.Name] -ne $_.Count) { $same = $false } }
    Assert-Check ($same -and ($manifestCounts.Values | Measure-Object -Sum).Sum -eq $entries.Count) 'sommaire identique au manifeste'
    Assert-Check ($manifest.structure.sha256 -match '^[0-9a-f]{64}$') 'empreinte de structure enregistree'
    Assert-Check ($manifest.empreinte_09.sha256 -match '^[0-9a-f]{64}$') 'empreinte 09 enregistree'
} catch [GeonafRefusal] {
    Write-Host $_.Exception.Message
    exit 1
}

if ($failures.Count -gt 0) { Write-Host "VERIFICATION EN ECHEC : $($failures.Count) controle(s)"; exit 1 }
Write-Host 'VERIFICATION CONFORME'
exit 0
