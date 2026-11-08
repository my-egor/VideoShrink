$ErrorActionPreference = 'Stop'
$Project = $PSScriptRoot
$Tools = Join-Path $Project '.tools'
$Archive = Join-Path $Tools 'nsis-3.13-2-any.pkg.tar.zst'
$Destination = Join-Path $Tools 'NSIS-MSYS2'
$VersionDirectory = Join-Path $Destination 'clang64'
$ExpectedSha256 = 'C7023E481C2B54F3DE4B588FC9843154D4E87CE851D7CBBDFCE6069D9B0307FF'
$Url = 'https://mirror.msys2.org/mingw/clang64/mingw-w64-clang-x86_64-nsis-3.13-2-any.pkg.tar.zst'

New-Item -ItemType Directory -Force -Path $Tools, (Join-Path $Project 'licenses') | Out-Null
if (Test-Path -LiteralPath $Archive) {
    $ActualSha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
    if ($ActualSha256 -ne $ExpectedSha256) { Remove-Item -LiteralPath $Archive -Force }
}
if (-not (Test-Path -LiteralPath $Archive)) {
    Invoke-WebRequest -Uri $Url -OutFile $Archive
    $ActualSha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
}
if ($ActualSha256 -ne $ExpectedSha256) {
    Remove-Item -LiteralPath $Archive -Force
    throw "NSIS 3.13 archive SHA-256 mismatch. Expected $ExpectedSha256, got $ActualSha256."
}

if (Test-Path -LiteralPath $VersionDirectory) {
    $ResolvedVersion = (Resolve-Path -LiteralPath $VersionDirectory).Path
    $ResolvedDestination = (Resolve-Path -LiteralPath $Destination).Path
    if (-not $ResolvedVersion.StartsWith($ResolvedDestination + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'NSIS cleanup path escaped the project tools directory.'
    }
    Remove-Item -LiteralPath $ResolvedVersion -Recurse -Force
}
$Tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $Tar) { throw 'Windows tar.exe is required to extract the verified NSIS package.' }
& $Tar.Source -xf $Archive -C $Destination
if ($LASTEXITCODE -ne 0) { throw 'Could not extract the verified NSIS package.' }

$Compiler = Join-Path $VersionDirectory 'bin\makensis.exe'
$License = Join-Path $VersionDirectory 'share\licenses\nsis\COPYING'
if (-not (Test-Path -LiteralPath $Compiler) -or -not (Test-Path -LiteralPath $License)) {
    throw 'The verified package did not contain the expected NSIS compiler and license.'
}
Copy-Item -LiteralPath $License -Destination (Join-Path $Project 'licenses\NSIS-LICENSE.txt') -Force
Write-Host "NSIS 3.13 prepared at $VersionDirectory"
