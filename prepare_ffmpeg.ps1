$ErrorActionPreference = 'Stop'
$Project = $PSScriptRoot
$Tools = Join-Path $Project 'tools'
$DownloadRoot = Join-Path $Tools '.download'
$Download = Join-Path $DownloadRoot 'ffmpeg-9.0.2-essentials_build.zip'
$Parts = Join-Path $DownloadRoot 'parts'
$Url = 'https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip'
$ExpectedSha256 = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
$ExpectedLength = 114768076
$ChunkSize = 8MB
$Parallelism = 12

New-Item -ItemType Directory -Force -Path $Tools | Out-Null
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
New-Item -ItemType Directory -Force -Path $Parts | Out-Null

$existing = Get-Item -LiteralPath $Download -ErrorAction SilentlyContinue
if (-not $existing -or $existing.Length -ne $ExpectedLength -or (Get-FileHash -LiteralPath $Download -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedSha256) {
    Remove-Item -LiteralPath $Parts -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $Parts | Out-Null
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(10)
    $requests = [System.Collections.Generic.List[object]]::new()
    for ($start = 0; $start -lt $ExpectedLength; $start += $ChunkSize) {
        $end = [Math]::Min($start + $ChunkSize - 1, $ExpectedLength - 1)
        $message = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Url)
        $message.Headers.Range = [System.Net.Http.Headers.RangeHeaderValue]::new([long]$start, [long]$end)
        $task = $client.SendAsync($message, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        $requests.Add([pscustomobject]@{ Start = $start; End = $end; Message = $message; Task = $task })
    }
    try {
        [System.Threading.Tasks.Task]::WhenAll([System.Threading.Tasks.Task[]]@($requests | ForEach-Object { $_.Task })).GetAwaiter().GetResult()
        foreach ($request in $requests) {
            $response = $request.Task.Result
            if (-not $response.IsSuccessStatusCode -or $response.StatusCode -ne [Net.HttpStatusCode]::PartialContent) {
                throw "FFmpeg range download failed for bytes $($request.Start)-$($request.End): HTTP $([int]$response.StatusCode)"
            }
            $range = $response.Content.Headers.ContentRange
            if ($range.From -ne $request.Start -or $range.To -ne $request.End -or $range.Length -ne $ExpectedLength) {
                throw "Unexpected FFmpeg content range: $range"
            }
            $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            if ($bytes.Length -ne ($request.End - $request.Start + 1)) { throw 'Incomplete FFmpeg archive segment.' }
            [IO.File]::WriteAllBytes((Join-Path $Parts ("{0:D8}.part" -f $request.Start)), $bytes)
            $response.Dispose()
            $request.Message.Dispose()
        }
        $output = [IO.File]::Open($Download, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            Get-ChildItem -LiteralPath $Parts -Filter '*.part' | Sort-Object { [long]$_.BaseName } | ForEach-Object {
                $bytes = [IO.File]::ReadAllBytes($_.FullName)
                $output.Write($bytes, 0, $bytes.Length)
            }
        } finally { $output.Dispose() }
    } finally {
        foreach ($request in $requests) {
            if ($request.Task.IsCompletedSuccessfully) { $request.Task.Result.Dispose() }
            $request.Message.Dispose()
        }
        $client.Dispose()
        $handler.Dispose()
        Remove-Item -LiteralPath $Parts -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$actual = (Get-FileHash -LiteralPath $Download -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $ExpectedSha256) { throw "Pinned FFmpeg SHA-256 mismatch: $actual" }

$Extract = Join-Path $Tools '.extract-parallel'
Remove-Item -LiteralPath $Extract -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Extract | Out-Null
Expand-Archive -LiteralPath $Download -DestinationPath $Extract -Force
$package = Get-ChildItem -LiteralPath $Extract -Directory | Select-Object -First 1
if (-not $package) { throw 'The FFmpeg archive has an unexpected layout.' }
foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
    $source = Join-Path $package.FullName "bin\$name"
    if (-not (Test-Path $source)) { throw "Missing $name in the verified FFmpeg archive." }
    Copy-Item -LiteralPath $source -Destination (Join-Path $Tools $name) -Force
}
$license = Get-ChildItem -LiteralPath $package.FullName -Filter 'LICENSE*' -File -Recurse | Select-Object -First 1
if ($license) { Copy-Item -LiteralPath $license.FullName -Destination (Join-Path $Project 'licenses\FFmpeg-LICENSE.txt') -Force }
$readme = Get-ChildItem -LiteralPath $package.FullName -Filter 'README.txt' -File -Recurse | Select-Object -First 1
if ($readme) { Copy-Item -LiteralPath $readme.FullName -Destination (Join-Path $Project 'licenses\FFmpeg-BUILD-README.txt') -Force }
Remove-Item -LiteralPath $Extract -Recurse -Force
Write-Host "FFmpeg 9.0.2 essentials extracted and verified (SHA-256 $ExpectedSha256)."
& (Join-Path $Tools 'ffmpeg.exe') -version | Select-Object -First 1
& (Join-Path $Tools 'ffmpeg.exe') -filters 2>$null | Select-String 'libvmaf'
& (Join-Path $Tools 'ffmpeg.exe') -encoders 2>$null | Select-String 'libx264|libx265'
