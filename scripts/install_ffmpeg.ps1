# Downloads a static FFmpeg build into the project (scripts\ffmpeg-bin) and sets FFMPEG_BIN env var for the current session.
# Run from repo root in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_ffmpeg.ps1

$ErrorActionPreference = "Stop"

# Allow override via env; otherwise try current release essentials zip, then git nightly.
$urls = @()
if ($env:FFMPEG_URL) { $urls += $env:FFMPEG_URL }
$urls += @(
  "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
  "https://www.gyan.dev/ffmpeg/builds/ffmpeg-git-essentials.7z"
)

$destDir  = Join-Path $PSScriptRoot "ffmpeg-bin"
$zipPath  = Join-Path $PSScriptRoot "ffmpeg-download"

function Try-Download {
  param($url)
  Write-Host "Attempting download: $url"
  $ext = [IO.Path]::GetExtension($url)
  $outFile = "$zipPath$ext"
  try {
    Invoke-WebRequest -Uri $url -OutFile $outFile -UseBasicParsing
    return $outFile
  } catch {
    Write-Host "Download failed for $url : $_"
    return $null
  }
}

# If ffmpeg.exe already exists, skip download
$existingExe = Get-ChildItem -Path $destDir -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $existingExe) {
  $archive = $null
  foreach ($u in $urls) {
    $archive = Try-Download $u
    if ($archive) { break }
  }

  if (-not $archive) {
    Write-Error "All FFmpeg download attempts failed. Set FFMPEG_URL to a working archive."
    exit 1
  }

  if (Test-Path $destDir) { Remove-Item $destDir -Recurse -Force }
  New-Item -ItemType Directory -Path $destDir | Out-Null

  if ($archive.EndsWith(".7z")) {
    if (-not (Get-Command 7z -ErrorAction SilentlyContinue)) {
      Write-Error "7z not found. Install 7-Zip or provide a .zip URL via FFMPEG_URL."
      exit 1
    }
    & 7z x $archive "-o$destDir" -y | Out-Null
  } else {
    Expand-Archive -LiteralPath $archive -DestinationPath $destDir -Force
  }
  Remove-Item $archive -Force

  $existingExe = Get-ChildItem -Path $destDir -Recurse -Filter ffmpeg.exe | Select-Object -First 1
}

if (-not $existingExe) {
  Write-Error "ffmpeg.exe not found after extraction"
  exit 1
}

$ffmpegPath = $existingExe.FullName
Write-Host "FFmpeg installed at $ffmpegPath"
$env:FFMPEG_BIN = $ffmpegPath
Write-Host "FFMPEG_BIN set for this session."
# Persist to user environment
[System.Environment]::SetEnvironmentVariable("FFMPEG_BIN", $ffmpegPath, "User")
Write-Host "FFMPEG_BIN stored in your user environment."
Write-Host "If a shell is already open, restart it or run:"
Write-Host '$env:FFMPEG_BIN="' + $ffmpegPath + '"'
