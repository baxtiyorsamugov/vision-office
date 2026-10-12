param(
    [string]$VideoDirectory = "video",
    [ValidateRange(1, 30)][int]$SampleFps = 5,
    [ValidateSet(640, 960)][int]$ImageSize = 960
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not [System.IO.Path]::IsPathRooted($VideoDirectory)) {
    $VideoDirectory = Join-Path $root $VideoDirectory
}
$videos = (Resolve-Path -LiteralPath $VideoDirectory).Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$gallery = "/app/data/exports/video-gallery-$stamp.json"
$output = "/app/data/exports/video-review-$stamp"

Push-Location $root
try {
    # Override the application entrypoint: no migrations, workers or event dispatch.
    & docker compose run --rm --no-deps --entrypoint python -v "${root}:/offline:ro" ui `
        /offline/tools/analyze_videos.py --export-gallery $gallery
    if ($LASTEXITCODE -ne 0) { throw "Read-only gallery export failed" }
    & docker compose run --rm --no-deps --entrypoint python -v "${root}:/offline:ro" -v "${videos}:/recordings:ro" ui `
        /offline/tools/analyze_videos.py --gallery $gallery --input /recordings --output $output `
        --sample-fps $SampleFps --imgsz $ImageSize --device cpu --ffmpeg /usr/bin/ffmpeg
    if ($LASTEXITCODE -ne 0) { throw "Video analysis failed; partial results may be available" }
    Write-Host "Report: $(Join-Path $root "data\exports\video-review-$stamp\index.html")"
} finally {
    Pop-Location
}
