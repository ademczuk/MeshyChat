<#
.SYNOPSIS
HQB pulls MeshyChat backups from HQD, verifies every copy, and applies retention (E02,
operator decisions 2026-09-25 and 2026-09-26). Windows PowerShell 5.1; nothing else.

.DESCRIPTION
    powershell -NoProfile -ExecutionPolicy Bypass -File pull_to_hqb.ps1 `
        -Source http://192.168.50.20:17071 -Dest D:\meshy-backups

HQD writes <database>-YYYYMMDDTHHMMSSZ.dump and, AFTER it, the matching .manifest.json into
its export folder (python -m tools.backup backup), and serves that folder READ-ONLY over HTTP
(python -m tools.backup serve): GET /index.json is {"files": [{"name", "bytes", "mtime"}]},
listing only names matching ^[a-z0-9_]+-\d{8}T\d{6}Z\.(dump|manifest\.json)$, and GET /<name>
returns a listed file's bytes. That is the transport the operator chose on 2026-09-26: NO
CREDENTIAL of any kind, because a password relayed by hand was the part of the first design
that failed. -Source may also be a folder path, which is how the tests exercise the same
logic without a server, and how the script was first written (an SMB share).

  1. reads the source and never writes to it;
  2. plans retention over what HQB already has plus what the source offers, and copies only
     the new backups that plan keeps (a backup retention would delete at once is not copied);
  3. copies each dump to <name>.dump.partial, hashes the COPY (sha256 and size) against the
     manifest, and only then renames it; a copy that does not match is deleted and named in
     "failed", and the run exits 1;
  4. writes the manifest last, from the very bytes it verified, so a manifest present at HQB
     always names a dump that was verified at HQB;
  5. applies retention to HQB's folder: 14 daily + 4 weekly per database, the same rule as
     tools/backup/retention.py (tests/test_backup_offsite.py runs both and requires the same
     answer): the newest backup of each of the 14 most recent UTC days that have one, then
     the newest of each of the 4 most recent ISO weeks older than the oldest kept day; a
     backup stamped after -Now is kept and not counted;
  6. checks FRESHNESS: the newest backup HQB holds of each database (a future-stamped one
     does not count) must be at most -MaxAgeHours old (default 36), and HQB must hold at
     least one; otherwise the database is named in "stale". This is what catches HQD's
     backups stopping, and a HQD clock stepped back (its new backups land before the kept
     window, are listed in "outside_retention" and never copied, so nothing new arrives);
  7. prints ONE JSON line (also appended to <Dest>\pull-log.jsonl) and exits 0 when every copy
     verified and nothing is stale, 1 when any copy failed, 2 when a database is stale (and
     no copy failed), or 3 when the source cannot be read. "ok" is true only with exit 0.

It refuses (PowerShell parameter validation, exit 1, nothing read or deleted) a -KeepDaily
below 1, a -KeepWeekly below 0 and a -MaxAgeHours below 1, as retention.plan refuses them:
-KeepDaily 0 would delete every backup HQB holds.

What it verifies is TRANSPORT: the copy at HQB is the bytes the manifest describes. The
manifest is not signed, so a dump and manifest both rewritten on HQD pass. It does not
re-hash what it already holds.

WHO MAY READ rests on the network, not on this script: HQD binds the server to its LAN
address and its firewall admits HQB only. That is acceptable for the node-test server's
test data and is NOT a production pattern; production would need a request HQD verifies,
e.g. one signed with HQB's existing node key (ASDLC incident 2026-09-26). The HTTP client
here uses NO PROXY, so a system proxy never sees a backup.
This channel does not sign requests yet. Until it does, who can read rests on the
network scope alone, which is not a production pattern; signing each request is
the planned change.

-Retired names databases that are no longer backed up (comma-separated, matched by exact
name). A retired database's backups are still copied if offered and kept by retention, but
its age is not judged, and "retired" in the result lists them. When the source cannot be
read and every database HQB holds is retired, nothing is expected from it, so the run is ok
(exit 0) with a note; while HQB holds any database that is not retired, it is exit 3 as
before. First use, 2026-10-04 (kxxx's ruling): meshy_nodetest, whose server was stopped under
decision 72 and whose backups the operator ruled off on 09-29, so its newest dump will never
be younger and its export is kept down on purpose. Bring a database back by dropping it here.

It takes no credential and needs no database access. -Now (yyyy-MM-ddTHH:mm:ssZ) fixes the
clock for tests.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Dest,
    [ValidateRange(1, [int]::MaxValue)][int]$KeepDaily = 14,
    [ValidateRange(0, [int]::MaxValue)][int]$KeepWeekly = 4,
    [ValidateRange(1, [int]::MaxValue)][int]$MaxAgeHours = 36,
    [string]$Now = '',
    [string[]]$Retired = @()
)
# -File hands "a,b" over as ONE string, so split here whatever form it arrives in.
$RetiredSet = @($Retired | ForEach-Object { "$_" -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Format = 'meshychat-backup/1'
$Suffix = '.manifest.json'
# The db part excludes \ / and : (txxxx, review of d4b922a). No Windows file name can
# contain them, so from a folder this changes nothing, and Python's rule and this one still
# agree on every name a folder can hold; over HTTP it is a SECOND check, independent of
# $HttpNamePattern, so a name that climbs out of -Dest is refused by two lines, not one.
$NamePattern = '^(?<db>[^\\/:]+)-(?<stamp>\d{8}T\d{6}Z)\.manifest\.json$'
$Inv = [Globalization.CultureInfo]::InvariantCulture
$Utc = [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal

function Get-Entry([string]$name) {
    $m = [regex]::Match($name, $NamePattern)
    if (-not $m.Success) { return $null }
    $ts = [DateTime]::MinValue
    if (-not [DateTime]::TryParseExact($m.Groups['stamp'].Value, "yyyyMMdd'T'HHmmss'Z'", $Inv, $Utc, [ref]$ts)) {
        return $null
    }
    return [pscustomobject]@{
        Name = $name
        Db   = $m.Groups['db'].Value
        Ts   = $ts
        Base = $name.Substring(0, $name.Length - $Suffix.Length)
    }
}

function Get-Entries([string]$folder) {
    $out = New-Object System.Collections.ArrayList
    foreach ($f in @(Get-ChildItem -LiteralPath $folder -File -Filter "*$Suffix")) {
        $e = Get-Entry $f.Name
        if ($null -ne $e) { [void]$out.Add($e) }
    }
    return ,$out
}

# ------------------------------------------------------------------ the source
# A folder (the tests, and the original SMB design) or HQD's read-only HTTP server. Every
# read of the source goes through these three functions, so the verification below is the
# same code for both.

# The server's name rule (tools/backup serve), applied AGAIN here: over HTTP the names come
# from the server, and a name is joined onto -Dest, so one like "..\x-...manifest.json" would
# write outside it. A folder listing cannot produce such a name; a server response can.
$HttpNamePattern = '^[a-z0-9_]+-\d{8}T\d{6}Z\.(dump|manifest\.json)$'

function Test-SourceFile([string]$name) {
    if ($IsHttp) { return $SourceNames.Contains($name) }
    return (Test-Path -LiteralPath (Join-Path $Source $name) -PathType Leaf)
}

function Read-SourceBytes([string]$name) {
    if (-not $IsHttp) { return ,[IO.File]::ReadAllBytes((Join-Path $Source $name)) }
    $r = Invoke-WebRequest -UseBasicParsing -Uri "$Base/$name" -TimeoutSec 60 -MaximumRedirection 0
    return ,$r.RawContentStream.ToArray()
}

function Copy-SourceFile([string]$name, [string]$destination) {
    if (-not $IsHttp) {
        Copy-Item -LiteralPath (Join-Path $Source $name) -Destination $destination -Force
        return
    }
    Invoke-WebRequest -UseBasicParsing -Uri "$Base/$name" -OutFile $destination -TimeoutSec 600 -MaximumRedirection 0
}

# The rule of tools/backup/retention.py, line for line. Returns the set of names to keep.
# -CaseSensitive: Python groups "meshy" and "MESHY" apart, and so must this.
function Get-KeepSet($entries, [DateTime]$now, [int]$keepDaily, [int]$keepWeekly) {
    $keep = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($group in @($entries | Group-Object -Property Db -CaseSensitive)) {
        foreach ($e in $group.Group) {
            if ($e.Ts -gt $now) { [void]$keep.Add($e.Name) }
        }
        $past = @($group.Group | Where-Object { $_.Ts -le $now } |
                  Sort-Object -Property @{ Expression = 'Ts'; Descending = $true },
                                        @{ Expression = 'Name'; Descending = $true })
        $days = New-Object 'System.Collections.Generic.List[DateTime]'
        foreach ($e in $past) {
            $day = $e.Ts.Date
            if ((-not $days.Contains($day)) -and $days.Count -lt $keepDaily) {
                $days.Add($day)
                [void]$keep.Add($e.Name)
            }
        }
        if ($days.Count -eq 0) { continue }
        $oldest = $days[$days.Count - 1]
        $weeks = New-Object 'System.Collections.Generic.List[DateTime]'
        foreach ($e in $past) {
            $day = $e.Ts.Date
            if ($day -lt $oldest) {
                $monday = $day.AddDays(-((([int]$day.DayOfWeek) + 6) % 7))
                if ((-not $weeks.Contains($monday)) -and $weeks.Count -lt $keepWeekly) {
                    $weeks.Add($monday)
                    [void]$keep.Add($e.Name)
                }
            }
        }
    }
    return ,$keep
}

function Get-Sha256Hex([string]$path) {
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-Result($result, [int]$code) {
    $line = $result | ConvertTo-Json -Compress -Depth 4
    if (Test-Path -LiteralPath $Dest -PathType Container) {
        Add-Content -LiteralPath (Join-Path $Dest 'pull-log.jsonl') -Value $line -Encoding UTF8
    }
    Write-Output $line
    exit $code
}

if ($Now) {
    $nowUtc = [DateTime]::ParseExact($Now, "yyyy-MM-dd'T'HH:mm:ss'Z'", $Inv, $Utc)
} else {
    $nowUtc = [DateTime]::UtcNow
}

# A source that cannot be read is a failure (exit 3) unless every database HQB holds is retired:
# then nothing is expected from it, and the run is ok with a note saying so.
function Exit-Unreachable([string]$message) {
    if ($RetiredSet.Count -gt 0 -and (Test-Path -LiteralPath $Dest -PathType Container)) {
        $heldDbs = Get-Entries $Dest          # assigned first: piping the call would pass the list whole
        $dbs = @($heldDbs | ForEach-Object { $_.Db })
        $live = @($dbs | Where-Object { $RetiredSet -cnotcontains $_ })
        if ($dbs.Count -gt 0 -and $live.Count -eq 0) {
            Write-Result ([ordered]@{
                ok      = $true
                source  = $Source
                dest    = $Dest
                now     = $nowUtc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $Inv)
                retired = @($RetiredSet)
                note    = "every database HQB holds is retired, so nothing is expected from the source: $message"
            }) 0
        }
    }
    Write-Result ([ordered]@{ ok = $false; error = $message }) 3
}

$IsHttp = $Source -match '^https?://'
$SourceNames = New-Object 'System.Collections.Generic.HashSet[string]'
$Base = $Source.TrimEnd('/')
if ($IsHttp) {
    # NO PROXY: the source is a LAN address, and a system proxy must never see a backup.
    [System.Net.WebRequest]::DefaultWebProxy = $null
    $ProgressPreference = 'SilentlyContinue'      # 5.1's progress bar slows downloads badly
    $doc = $null
    $raw = $null
    try {
        # -MaximumRedirection 0 on every request here (txxxx): a redirect is never followed,
        # so the firewall scope on HQD stays the whole story of who is being read from.
        $idx = Invoke-WebRequest -UseBasicParsing -Uri "$Base/index.json" -TimeoutSec 60 -MaximumRedirection 0
        $raw = [Text.Encoding]::UTF8.GetString($idx.RawContentStream.ToArray())
    } catch {
        Exit-Unreachable "source not reachable: $Source ($($_.Exception.Message))"
    }
    # A server that ANSWERS with something else is a different failure from one that does not
    # answer, so the parse is outside the fetch's catch: both exit 3, and the message says which.
    try { $doc = $raw | ConvertFrom-Json } catch { $doc = $null }
    # The contract is {"files": [{"name": ...}, ...]}; anything else is not a source this
    # script can read, and it says so rather than reading it as an empty folder.
    $filesProp = if ($null -ne $doc) { $doc.PSObject.Properties['files'] } else { $null }
    if ($null -eq $filesProp -or -not ($filesProp.Value -is [System.Array])) {
        Write-Result ([ordered]@{ ok = $false; error = "source index is not {`"files`": [...]}: $Source/index.json" }) 3
    }
    foreach ($item in $filesProp.Value) {
        $nameProp = if ($null -ne $item) { $item.PSObject.Properties['name'] } else { $null }
        if ($null -ne $nameProp -and $nameProp.Value -is [string] -and
            [regex]::IsMatch($nameProp.Value, $HttpNamePattern)) {
            [void]$SourceNames.Add($nameProp.Value)
        }
    }
} elseif (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    Exit-Unreachable "source folder not reachable: $Source"
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
foreach ($p in @(Get-ChildItem -LiteralPath $Dest -File -Filter '*.partial')) {
    Remove-Item -LiteralPath $p.FullName -Force      # a previous run's, never a backup
}

$copied = New-Object 'System.Collections.Generic.List[string]'
$failed = New-Object 'System.Collections.Generic.List[string]'
$outside = New-Object 'System.Collections.Generic.List[string]'
$present = 0

$have = Get-Entries $Dest
$haveNames = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($e in $have) { [void]$haveNames.Add($e.Name) }
if ($IsHttp) {
    $offered = New-Object System.Collections.ArrayList
    foreach ($n in $SourceNames) {
        $e = Get-Entry $n
        if ($null -ne $e) { [void]$offered.Add($e) }
    }
} else {
    $offered = Get-Entries $Source
}
$union = New-Object System.Collections.ArrayList
foreach ($e in $have) { [void]$union.Add($e) }
foreach ($e in $offered) { if (-not $haveNames.Contains($e.Name)) { [void]$union.Add($e) } }
$wanted = Get-KeepSet $union $nowUtc $KeepDaily $KeepWeekly

foreach ($e in @($offered | Sort-Object -Property Name)) {
    if ($haveNames.Contains($e.Name)) { $present++; continue }
    if (-not $wanted.Contains($e.Name)) { $outside.Add($e.Name); continue }
    $partial = $null
    try {
        $bytes = Read-SourceBytes $e.Name
        $m = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        if ($m.format -ne $Format) { throw "format '$($m.format)', expected '$Format'" }
        $dumpName = "$($e.Base).dump"
        if ($m.dump_file -cne $dumpName) { throw "names dump_file '$($m.dump_file)', expected '$dumpName'" }
        if ($m.dump_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'no dump_sha256' }
        if (-not (Test-SourceFile $dumpName)) { throw "no $dumpName at the source" }
        $partial = Join-Path $Dest "$dumpName.partial"
        Copy-SourceFile $dumpName $partial
        $size = (Get-Item -LiteralPath $partial).Length
        $hash = Get-Sha256Hex $partial
        if ($size -ne [int64]$m.dump_bytes -or $hash -ne $m.dump_sha256) {
            throw "the copy at HQB does not match the manifest (sha256 $hash, $size bytes; manifest sha256 $($m.dump_sha256), $($m.dump_bytes) bytes)"
        }
        Move-Item -LiteralPath $partial -Destination (Join-Path $Dest $dumpName) -Force
        $partial = Join-Path $Dest "$($e.Name).partial"
        [IO.File]::WriteAllBytes($partial, $bytes)
        Move-Item -LiteralPath $partial -Destination (Join-Path $Dest $e.Name) -Force
        $partial = $null
        $copied.Add($e.Name)
    } catch {
        if ($null -ne $partial -and (Test-Path -LiteralPath $partial)) {
            Remove-Item -LiteralPath $partial -Force
        }
        $failed.Add("$($e.Name): $($_.Exception.Message)")
    }
}

# Retention over what HQB actually holds now (a failed copy is not counted).
$held = Get-Entries $Dest
$keep = Get-KeepSet $held $nowUtc $KeepDaily $KeepWeekly
$deleted = New-Object 'System.Collections.Generic.List[string]'
foreach ($e in @($held | Sort-Object -Property Name)) {
    if ($keep.Contains($e.Name)) { continue }
    Remove-Item -LiteralPath (Join-Path $Dest $e.Name) -Force          # the manifest first
    $dump = Join-Path $Dest "$($e.Base).dump"
    if (Test-Path -LiteralPath $dump) { Remove-Item -LiteralPath $dump -Force }
    $deleted.Add($e.Name)
}
$future = New-Object 'System.Collections.Generic.List[string]'
foreach ($e in $held) { if ($e.Ts -gt $nowUtc) { $future.Add($e.Name) } }

# Freshness, per database, over what HQB holds now (a future stamp proves nothing).
$stale = New-Object 'System.Collections.Generic.List[string]'
$heldNow = Get-Entries $Dest          # assigned first: piping the call would pass the list whole
$remaining = @($heldNow | Where-Object { $_.Ts -le $nowUtc })
$note = ''
if ($outside.Count -gt 0) {
    $note = "; the source offers $($outside.Count) backup(s) outside retention, e.g. $($outside[0]) (a HQD clock stepped back puts its new backups there)"
}
if ($remaining.Count -eq 0) {
    $stale.Add("no backup held at HQB$note")
}
foreach ($group in @($remaining | Group-Object -Property Db -CaseSensitive)) {
    if ($RetiredSet -ccontains $group.Name) { continue }          # retired: its age is not judged
    $newest = @($group.Group | Sort-Object -Property Ts -Descending)[0]
    $age = ($nowUtc - $newest.Ts).TotalHours
    if ($age -gt $MaxAgeHours) {
        $stale.Add(("{0}: the newest backup held is {1}, {2:N1} hours old (limit {3}){4}" -f $group.Name, $newest.Name, $age, $MaxAgeHours, $note))
    }
}

$result = [ordered]@{
    ok                = ($failed.Count -eq 0 -and $stale.Count -eq 0)
    source            = $Source
    dest              = $Dest
    now               = $nowUtc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $Inv)
    copied            = @($copied)
    already_present   = $present
    outside_retention = @($outside)
    failed            = @($failed)
    deleted           = @($deleted)
    kept              = $keep.Count
    future            = @($future)
    stale             = @($stale)
    max_age_hours     = $MaxAgeHours
    retired           = @($RetiredSet)
}
if ($failed.Count -gt 0) { Write-Result $result 1 }
if ($stale.Count -gt 0) { Write-Result $result 2 }
Write-Result $result 0
