<#
Replay recorded camera files as live RTSP cameras through an isolated copy of
Vision Office (same code, same thresholds, snapshot of the production gallery).
Production containers, database and EduSchool/backend delivery are untouched.

  .\tools\replay_recordings.ps1                       # start (CPU), recordings in .\video
  .\tools\replay_recordings.ps1 -Device cuda          # start on GPU if enough free VRAM
  .\tools\replay_recordings.ps1 -Loop                 # loop recordings forever (default: each plays once)
  .\tools\replay_recordings.ps1 -Camera kirish        # only one recording (faster, no lag); "kirish,chiqish1" for two
  .\tools\replay_recordings.ps1 -RefreshDb            # take a fresh snapshot of THIS PC's database first
  .\tools\replay_recordings.ps1 -Snapshot D:\work.dump # use a pg_dump -Fc file copied from the work PC
  .\tools\replay_recordings.ps1 -Action View          # smooth live window(s) with boxes and names
  .\tools\replay_recordings.ps1 -Action Report        # HTML report of hits and misses
  .\tools\replay_recordings.ps1 -Action Review        # photo-review page of near-misses (same flow as before)
  .\tools\replay_recordings.ps1 -Action Compare -Device cuda   # every recording alone: prod vs candidate settings
  .\tools\replay_recordings.ps1 -Action Logs          # follow the replay worker log
  .\tools\replay_recordings.ps1 -Action Stop          # stop the bench (keeps its database)
  .\tools\replay_recordings.ps1 -Action Purge         # remove the bench, its database and outputs
#>
param(
    [ValidateSet("Start", "Stop", "View", "Report", "Review", "Compare", "Logs", "Purge")][string]$Action = "Start",
    [string]$VideoDirectory = "video",
    [ValidateSet("cpu", "cuda")][string]$Device = "cpu",
    [switch]$RefreshDb,
    [string]$Snapshot = "",
    [string]$Camera = "",
    [ValidateRange(0, 32)][int]$CpuThreads = 0,
    [switch]$Transcode,
    [switch]$Loop,
    [switch]$AllRuns,
    [double]$Minutes = 0,
    [ValidateRange(500, 4000)][int]$MinFreeVramMiB = 1500,
    [double]$MinSimilarity = 0.40,
    [double]$UnknownRetrySec = 1.5,
    [double]$FaceDetThresh = 0.5,
    [double]$MinMargin = 0,
    [switch]$NoView,
    [ValidateRange(1, 5)][int]$Repeat = 1,
    [double]$CandidateRetrySec = 0.4,
    [double]$CandidateMinMargin = 0.05,
    [string]$PersonsDir = ""
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $root "replay\runtime"
$snapshotFile = Join-Path $runtime "snapshot\production.dump"
$restoredMarker = Join-Path $runtime "snapshot\restored.txt"
$replayFiles = @("-f", "docker-compose.replay.yml")

# Simple function on purpose: docker flags such as -f/-T/-d stay in $args.
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($args -join ' ') failed ($LASTEXITCODE)" }
}

function Test-Image([string]$Name) {
    & docker image inspect $Name *> $null
    return $LASTEXITCODE -eq 0
}

Push-Location $root
try {
    $osType = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $osType -ne 'linux') { throw "Start Docker Desktop (Linux containers / WSL2) first." }

    if (-not [IO.Path]::IsPathRooted($VideoDirectory)) { $VideoDirectory = Join-Path $root $VideoDirectory }
    if (Test-Path -LiteralPath $VideoDirectory) { $VideoDirectory = (Resolve-Path -LiteralPath $VideoDirectory).Path }
    $env:REPLAY_VIDEO_DIR = $VideoDirectory
    $env:REPLAY_TRANSCODE = if ($Transcode) { "1" } else { "0" }
    $env:REPLAY_LOOP = if ($Loop) { "1" } else { "0" }
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $env:REPLAY_UNKNOWN_RETRY_SEC = $UnknownRetrySec.ToString($inv)
    $env:REPLAY_FACE_DET_THRESH = $FaceDetThresh.ToString($inv)
    $env:REPLAY_MIN_MATCH_MARGIN = $MinMargin.ToString($inv)
    $runMarker = Join-Path $runtime "data\run_started.txt"
    $snapshotSource = Join-Path $runtime "snapshot\source.txt"

    function Open-LiveWindows([int]$Count) {
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    for ($i = 0; $i -lt [Math]::Max(1, $Count); $i++) {
        $url = "http://127.0.0.1:$(8090 + $i)/"
        Write-Host "  LIVE: $url" -ForegroundColor Green
        if ($edge) { Start-Process $edge -ArgumentList "--app=$url", "--new-window" } else { Start-Process $url }
    }
}

    switch ($Action) {
        "View" {
            $count = @(& docker compose @replayFiles logs replay-worker 2>$null | Select-String "LIVE VIEW").Count
            Open-LiveWindows ([Math]::Min(6, [Math]::Max(1, $count)))
            return
        }
        "Review" {
            # Export the bench results like a work-PC export (one dump + events + persons) and
            # build the usual review page with build_similarity_review.py.
            if (-not (Test-Path -LiteralPath $runMarker)) { throw "Run the bench first (no run_started.txt)." }
            $since = ([datetime]::Parse((Get-Content -LiteralPath $runMarker -Raw).Trim()).ToUniversalTime()).ToString("yyyy-MM-ddTHH:mmZ")
            if (-not $PersonsDir -and (Test-Path -LiteralPath $snapshotSource)) {
                $candidate = Join-Path (Split-Path -Parent (Get-Content -LiteralPath $snapshotSource -Raw).Trim()) "persons"
                if (Test-Path -LiteralPath $candidate) { $PersonsDir = $candidate }
            }
            if (-not $PersonsDir) {
                # Newest work-PC export first: its persons/ matches the gallery in the snapshot.
                $latest = Get-ChildItem (Join-Path $root "data\imports") -Directory -Filter "review-*" -ErrorAction SilentlyContinue |
                    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "persons") } | Sort-Object Name -Descending | Select-Object -First 1
                $PersonsDir = if ($latest) { Join-Path $latest.FullName "persons" } else { Join-Path $root "data\persons" }
            }
            if (-not (Test-Path -LiteralPath $PersonsDir)) { throw "Reference photos folder not found: $PersonsDir (use -PersonsDir)" }
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $export = Join-Path $runtime "exports\replay-export-$stamp"
            $page = Join-Path $root "data\exports\replay-review-$stamp"
            New-Item -ItemType Directory -Force -Path $export | Out-Null
            Write-Host "1/3 Dumping the replay database (bench only)" -ForegroundColor Cyan
            Invoke-Docker compose @replayFiles exec -T replay-postgres pg_dump -U vision_office -Fc -f /tmp/replay-review.dump vision_office
            Invoke-Docker compose @replayFiles cp replay-postgres:/tmp/replay-review.dump (Join-Path $export "replay.dump")
            Write-Host "2/3 Copying replay frames and reference photos ($PersonsDir)" -ForegroundColor Cyan
            & robocopy (Join-Path $runtime "data\events") (Join-Path $export "events") /E /NFL /NDL /NJH /NJS /NP | Out-Null
            & robocopy $PersonsDir (Join-Path $export "persons") /E /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
            Write-Host "3/3 Building the review page (unknown faces since $since, similarity >= $MinSimilarity)" -ForegroundColor Cyan
            $python = Join-Path $root ".venv\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
            & $python (Join-Path $root "tools\build_similarity_review.py") --input-root $export --output $page `
                --since $since --min-similarity $MinSimilarity.ToString([Globalization.CultureInfo]::InvariantCulture)
            if ($LASTEXITCODE -ne 0) { throw "build_similarity_review.py failed ($LASTEXITCODE)" }
            $index = Join-Path $page "index.html"
            Write-Host "Open: $index" -ForegroundColor Green
            Start-Process $index
            return
        }
        "Compare" {
            # One recording at a time (a 4 GB laptop GPU cannot load four cameras fairly),
            # first with production settings, then with the candidate settings.
            $recordings = Get-ChildItem -LiteralPath $VideoDirectory -File |
                Where-Object { $_.Extension -match '^\.(mp4|avi|mkv|mov|ts|dav|h264|h265|264|265)$' -and $_.BaseName -match '^[A-Za-z0-9_-]{1,60}$' }
            if ($Camera) {
                $wantedCompare = @($Camera -split ',' | ForEach-Object { $_.Trim().ToLower() })
                $recordings = $recordings | Where-Object { $wantedCompare -contains $_.BaseName.ToLower() }
            }
            $profiles = @(
                @{ Name = "prod"; Retry = 1.5; Margin = 0 },
                @{ Name = "candidate"; Retry = $CandidateRetrySec; Margin = $CandidateMinMargin }
            )
            $runsFile = Join-Path $runtime "data\compare-runs-$(Get-Date -Format 'yyyyMMdd-HHmmss').jsonl"
            $total = @($recordings).Count * $profiles.Count * $Repeat
            $index = 0
            foreach ($recording in $recordings) {
                foreach ($round in 1..$Repeat) {
                    foreach ($profile in $profiles) {
                        $index++
                        Write-Host ""
                        Write-Host "=== $index/$total  $($recording.BaseName)  $($profile.Name)  (round $round) ===" -ForegroundColor Magenta
                        $startArgs = @{ Action = "Start"; Camera = $recording.BaseName; Device = $Device; NoView = $true
                                        UnknownRetrySec = $profile.Retry; MinMargin = $profile.Margin; VideoDirectory = $VideoDirectory }
                        if ($Snapshot -and $index -eq 1) { $startArgs.Snapshot = $Snapshot }
                        & $PSCommandPath @startArgs
                        $started = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
                        $cameraId = "replay_" + $recording.BaseName.ToLower()
                        $statusFile = Join-Path $runtime "data\runtime_status_$cameraId.json"
                        $seenConnected = $false; $disconnectedSince = $null
                        $deadline = (Get-Date).AddMinutes(8)
                        while ((Get-Date) -lt $deadline) {
                            Start-Sleep -Seconds 2
                            try { $status = Get-Content -LiteralPath $statusFile -Raw -ErrorAction Stop | ConvertFrom-Json } catch { continue }
                            if ($status.updated_at -lt $started) { continue }
                            if ($status.stream_status -eq "connected") { $seenConnected = $true; $disconnectedSince = $null }
                            elseif ($seenConnected) {
                                if (-not $disconnectedSince) { $disconnectedSince = Get-Date }
                                elseif (((Get-Date) - $disconnectedSince).TotalSeconds -ge 8) { break }
                            }
                        }
                        if (-not $seenConnected) { Write-Warning "$($recording.BaseName): stream never connected in this run." }
                        $ended = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
                        $params = Get-Content -LiteralPath (Join-Path $runtime "data\run_params.txt") -Raw
                        $record = @{ camera = $cameraId; profile = $profile.Name; round = $round; start = $started; end = $ended; params = $params.Trim() }
                        Add-Content -LiteralPath $runsFile -Value ($record | ConvertTo-Json -Compress)
                    }
                }
            }
            Invoke-Docker compose @replayFiles stop replay-worker replay-rtsp
            $python = Join-Path $root ".venv\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
            $output = Join-Path $runtime "data\exports\replay-compare-$(Get-Date -Format 'yyyyMMdd-HHmmss').html"
            & $python (Join-Path $root "tools\replay\compare_runs.py") --data (Join-Path $runtime "data") --runs $runsFile --output $output
            Start-Process $output
            return
        }
        "Stop" { Invoke-Docker compose @replayFiles down; return }
        "Purge" {
            Invoke-Docker compose @replayFiles down -v
            if (Test-Path -LiteralPath $runtime) { Remove-Item -LiteralPath $runtime -Recurse -Force }
            Write-Host "Replay bench removed." -ForegroundColor Green
            return
        }
        "Logs" { & docker compose @replayFiles logs -f --tail 200 replay-worker replay-rtsp; return }
        "Report" {
            $summaryArgs = @("--minutes", $Minutes.ToString([Globalization.CultureInfo]::InvariantCulture))
            # By default only the latest run is counted, so repeated runs never inflate the numbers.
            if (-not $AllRuns -and $Minutes -le 0 -and (Test-Path -LiteralPath $runMarker)) {
                $summaryArgs += @("--since", (Get-Content -LiteralPath $runMarker -Raw).Trim())
            }
            Invoke-Docker compose @replayFiles exec -T replay-ui python tools/replay/summary.py @summaryArgs
            $latest = Get-ChildItem (Join-Path $runtime "data\exports") -Directory -Filter "replay-report-*" |
                Sort-Object Name -Descending | Select-Object -First 1
            if ($latest) {
                $index = Join-Path $latest.FullName "index.html"
                Write-Host "Open: $index" -ForegroundColor Green
                Start-Process $index
            }
            return
        }
    }

    # ---- Start ----
    if (-not (Test-Path -LiteralPath $VideoDirectory)) { throw "Recordings folder not found: $VideoDirectory" }
    foreach ($image in @("vision-office-ui", "vision-office-vision-worker")) {
        if (-not (Test-Image $image)) { throw "Image $image not found. Start production once with .\start_vision_office.ps1." }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $runtime "config"), (Join-Path $runtime "data"), (Join-Path $runtime "snapshot") | Out-Null

    $onlyArgs = @()
    if ($Camera) { $onlyArgs = @("--only", $Camera) }
    # One camera gets more CPU threads so it keeps up in real time.
    if ($CpuThreads -le 0) {
        $CpuThreads = if ($Camera -and ($Camera -split ',').Count -eq 1) { [Math]::Max(2, [Math]::Min(6, [Environment]::ProcessorCount - 2)) } else { 2 }
    }
    $env:REPLAY_CPU_THREADS = "$CpuThreads"
    Write-Host "1/4 Building replay camera list from $VideoDirectory" -ForegroundColor Cyan
    Invoke-Docker run --rm --entrypoint python `
        -v "${root}\config:/prod-config:ro" -v "${root}\tools:/replay-tools:ro" `
        -v "${VideoDirectory}:/recordings:ro" -v "${runtime}\config:/out" `
        vision-office-ui /replay-tools/replay/make_config.py `
        --production /prod-config/settings.yaml --recordings /recordings --output /out/settings.yaml @onlyArgs

    if ($Snapshot) {
        if (-not (Test-Path -LiteralPath $Snapshot)) { throw "Snapshot not found: $Snapshot" }
        Write-Host "2/4 Using database snapshot $Snapshot" -ForegroundColor Cyan
        Copy-Item -LiteralPath $Snapshot -Destination $snapshotFile -Force
        Set-Content -LiteralPath $snapshotSource -Value (Resolve-Path -LiteralPath $Snapshot).Path
        if (Test-Path -LiteralPath $restoredMarker) { Remove-Item -LiteralPath $restoredMarker }
    } elseif ($RefreshDb -or -not (Test-Path -LiteralPath $snapshotFile)) {
        Write-Host "2/4 Snapshot of this PC's database (read-only pg_dump). For the work PC gallery use -Snapshot." -ForegroundColor Cyan
        Invoke-Docker compose -f docker-compose.yml exec -T postgres sh -c 'pg_dump -U $POSTGRES_USER -d $POSTGRES_DB -Fc -f /tmp/replay-snapshot.dump'
        Invoke-Docker compose -f docker-compose.yml cp postgres:/tmp/replay-snapshot.dump $snapshotFile
        & docker compose -f docker-compose.yml exec -T postgres rm -f /tmp/replay-snapshot.dump | Out-Null
        if (Test-Path -LiteralPath $restoredMarker) { Remove-Item -LiteralPath $restoredMarker }
    } else {
        Write-Host "2/4 Using existing snapshot $snapshotFile (-RefreshDb for a new one)" -ForegroundColor Cyan
    }

    Invoke-Docker compose @replayFiles up -d --wait replay-postgres
    if (-not (Test-Path -LiteralPath $restoredMarker)) {
        Write-Host "3/4 Restoring snapshot into the replay database" -ForegroundColor Cyan
        & docker compose @replayFiles exec -T replay-postgres pg_restore --clean --if-exists --no-owner --no-privileges `
            -U vision_office -d vision_office /snapshot/production.dump
        if ($LASTEXITCODE -ne 0) { Write-Warning "pg_restore reported warnings (usually harmless on the first restore)." }
        Set-Content -LiteralPath $restoredMarker -Value (Get-Date -Format s)
    } else {
        Write-Host "3/4 Replay database already restored" -ForegroundColor Cyan
    }

    $files = $replayFiles
    if ($Device -eq "cuda") {
        $free = 0
        $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if ($smi) { $free = [int]((& $smi.Source --query-gpu=memory.free --format=csv,noheader,nounits | Select-Object -First 1).Trim()) }
        $gpuImage = @("cu124", "cu128") | Where-Object { Test-Image "vision-office-gpu:$_" } | Select-Object -First 1
        if (-not $gpuImage) {
            Write-Warning "No GPU image found; using CPU."
        } elseif ($free -lt $MinFreeVramMiB) {
            Write-Warning "Only $free MiB VRAM free (production worker shares the card); using CPU. Lower -MinFreeVramMiB at your own risk."
        } else {
            $env:VISION_OFFICE_GPU_PROFILE = $gpuImage
            $files = $replayFiles + @("-f", "docker-compose.replay.gpu.yml")
            Write-Host "GPU: $gpuImage, $free MiB free" -ForegroundColor Cyan
        }
    }

    Write-Host "4/4 Starting replay RTSP server, worker and dashboard" -ForegroundColor Cyan
    Set-Content -LiteralPath $runMarker -Value ((Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"))
    $params = "camera=$Camera unknown_retry_sec=$($env:REPLAY_UNKNOWN_RETRY_SEC) face_det_thresh=$($env:REPLAY_FACE_DET_THRESH) min_margin=$($env:REPLAY_MIN_MATCH_MARGIN) device=$Device"
    Set-Content -LiteralPath (Join-Path $runtime "data\run_params.txt") -Value $params
    Write-Host "Run: $params" -ForegroundColor Cyan
    if (-not $Loop) { Write-Host "Each recording plays ONCE (about its own length). Then run -Action Report." -ForegroundColor Yellow }
    Invoke-Docker compose @files up -d --no-build --force-recreate replay-rtsp replay-worker replay-view replay-api replay-ui

    Write-Host ""
    Write-Host "Replay dashboard: http://127.0.0.1:8502   (production stays on :8501)" -ForegroundColor Green
    $wanted = @($Camera -split ',' | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
    Get-ChildItem -LiteralPath $VideoDirectory -File | Where-Object { $_.Extension -match '^\.(mp4|avi|mkv|mov|ts|dav|h264|h265|264|265)$' } |
        Where-Object { $wanted.Count -eq 0 -or $wanted -contains $_.BaseName.ToLower() } | ForEach-Object {
        Write-Host ("  VLC: rtsp://127.0.0.1:8554/{0}" -f $_.BaseName)
    }
    if ($NoView) { return }
    Write-Host "Waiting for the live view (model warm-up)..." -ForegroundColor Cyan
    $cameraCount = @(Get-Content -LiteralPath (Join-Path $runtime "config\settings.yaml") | Select-String "rtsp_url:").Count
    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 3
        $ready = @(& docker compose @replayFiles logs replay-worker 2>$null | Select-String "LIVE VIEW").Count
    } while ($ready -lt $cameraCount -and (Get-Date) -lt $deadline)
    if ($ready -gt 0) { Open-LiveWindows $ready } else { Write-Warning "Live view not up yet. Later: .\tools\replay_recordings.ps1 -Action View" }
    Write-Host "Live window again: .\tools\replay_recordings.ps1 -Action View"
    Write-Host "Report:  .\tools\replay_recordings.ps1 -Action Report"
    Write-Host "Logs:    .\tools\replay_recordings.ps1 -Action Logs"
    Write-Host "Stop:    .\tools\replay_recordings.ps1 -Action Stop"
}
finally {
    Pop-Location
}
