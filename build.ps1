$ErrorActionPreference = 'Stop'
$Project = $PSScriptRoot
$VenvPython = Join-Path $Project '.venv\Scripts\python.exe'
$Python = if (Test-Path $VenvPython) { $VenvPython } else { (Get-Command python -ErrorAction Stop).Source }
$Tools = Join-Path $Project 'tools'
$Dist = Join-Path $Project 'dist'
$AppDist = Join-Path $Dist 'VideoShrink'
$Version = '1.1.6'

if (-not (Test-Path (Join-Path $Tools 'ffmpeg.exe')) -or -not (Test-Path (Join-Path $Tools 'ffprobe.exe'))) {
    throw 'Run prepare_ffmpeg.ps1 first to download and verify the pinned FFmpeg build.'
}
if (-not (Test-Path (Join-Path $Project 'licenses\FFmpeg-LICENSE.txt'))) {
    throw 'Add the exact FFmpeg license and redistribution notices under VideoShrink\licenses first.'
}
if (-not (Test-Path (Join-Path $Project 'licenses\THIRD-PARTY-SOURCES.txt'))) {
    throw 'Add exact FFmpeg and linked-library source information under VideoShrink\licenses first.'
}
if (-not (Test-Path (Join-Path $Project 'licenses\NSIS-LICENSE.txt'))) {
    throw 'Add the NSIS license text under VideoShrink\licenses first.'
}
if (-not (Test-Path (Join-Path $Project '.venv\Scripts\python.exe'))) {
    & $Python -m venv (Join-Path $Project '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project build environment.' }
    $Python = Join-Path $Project '.venv\Scripts\python.exe'
}
& $Python -m pip install --disable-pip-version-check -r (Join-Path $Project 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install pinned build dependency.' }
# Preserve the runtimes' redistribution notices alongside the frozen executable.
$PyInstallerLicense = Join-Path (Split-Path $Python -Parent | Split-Path -Parent) 'Lib\site-packages\PyInstaller\LICENSE.txt'
if (Test-Path $PyInstallerLicense) { Copy-Item -LiteralPath $PyInstallerLicense -Destination (Join-Path $Project 'licenses\PyInstaller-LICENSE.txt') -Force }
$BasePython = & $Python -c "import sys; print(sys.base_prefix)"
$PythonLicense = Join-Path $BasePython 'LICENSE.txt'
if (Test-Path $PythonLicense) { Copy-Item -LiteralPath $PythonLicense -Destination (Join-Path $Project 'licenses\Python-LICENSE.txt') -Force }
$TclLicense = Get-ChildItem (Join-Path $BasePython 'tcl') -Filter 'license.terms' -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($TclLicense) { Copy-Item -LiteralPath $TclLicense.FullName -Destination (Join-Path $Project 'licenses\TclTk-license.terms') -Force }
Push-Location $Project
try {
    & $Python -m py_compile 'run.py' 'videoshrink\app.py' 'videoshrink\core.py'
    if ($LASTEXITCODE -ne 0) { throw 'Python source compilation failed.' }
    & $Python -c 'import videoshrink.app, videoshrink.core'
    if ($LASTEXITCODE -ne 0) { throw 'VideoShrink modules could not be imported.' }
    & $Python -m PyInstaller --clean --noconfirm 'VideoShrink.spec'
}
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
if (-not (Test-Path -LiteralPath (Join-Path $AppDist 'VideoShrink.exe'))) {
    throw "PyInstaller did not create the application folder at $AppDist."
}
Copy-Item -LiteralPath (Join-Path $Project 'README.md') -Destination (Join-Path $AppDist 'README.txt') -Force

$Portable = Join-Path $Dist "VideoShrink_Portable_$Version"
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
if (Test-Path $Portable) {
    $resolvedPortable = (Resolve-Path -LiteralPath $Portable).Path
    $resolvedDist = (Resolve-Path -LiteralPath $Dist).Path
    if (-not $resolvedPortable.StartsWith($resolvedDist + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Portable cleanup path escaped dist.' }
    Remove-Item -LiteralPath $resolvedPortable -Recurse -Force
}
Copy-Item -LiteralPath $AppDist -Destination $Portable -Recurse
foreach ($Folder in @('Input','Output','Logs','Temp','licenses')) { New-Item -ItemType Directory -Force -Path (Join-Path $Portable $Folder) | Out-Null }
Set-Content -LiteralPath (Join-Path $Portable 'Input\Положите видео сюда.txt') `
    -Value 'Скопируйте сюда MP4-файлы, затем запустите VideoShrink.exe.' -Encoding utf8
Remove-Item -LiteralPath (Join-Path $Portable 'installed.json') -Force -ErrorAction SilentlyContinue
Set-Content -LiteralPath (Join-Path $Portable 'portable.json') -Value '{}' -Encoding utf8
foreach ($License in @('README.txt','FFmpeg-LICENSE.txt','FFmpeg-BUILD-README.txt','THIRD-PARTY-SOURCES.txt','NSIS-LICENSE.txt','PyInstaller-LICENSE.txt','Python-LICENSE.txt','TclTk-license.terms')) {
    $sourceLicense = Join-Path $Project "licenses\$License"
    if (Test-Path $sourceLicense) { Copy-Item -LiteralPath $sourceLicense -Destination (Join-Path $Portable "licenses\$License") }
}
Copy-Item -LiteralPath (Join-Path $Project 'README.md') -Destination (Join-Path $Portable 'README.txt')
$PortableZip = Join-Path $Dist "VideoShrink_Portable_$Version.zip"
Remove-Item -LiteralPath $PortableZip -Force -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory(
    $Portable, $PortableZip, [IO.Compression.CompressionLevel]::Fastest, $false)
$NsisCandidates = @(
    (Join-Path $Project '.tools\NSIS-MSYS2\clang64\bin\makensis.exe'),
    (Join-Path $Project '.tools\NSIS\nsis-3.13\makensis.exe'),
    (Join-Path $Project '.tools\NSIS\makensis.exe'),
    (Join-Path $env:ProgramFiles 'NSIS\makensis.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'NSIS\makensis.exe')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
$MakeNsis = $NsisCandidates | Select-Object -First 1
if (-not $MakeNsis) { throw "Portable archive created, but NSIS makensis.exe was not found. Run prepare_nsis.ps1; installer not created." }
if ($MakeNsis -eq (Join-Path $Project '.tools\NSIS-MSYS2\clang64\bin\makensis.exe')) {
    $env:NSISDIR = Join-Path $Project '.tools\NSIS-MSYS2\clang64\share\nsis'
}
Push-Location $Project
try { & $MakeNsis '/V2' 'installer.nsi' }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'NSIS compilation failed.' }
Write-Host "Portable ZIP and NSIS installer created in $Dist"
