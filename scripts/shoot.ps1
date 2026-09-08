# Screenshot pages of the running dashboard with headless Chrome.
#
# The IDE browser tooling is unreliable on this machine, and the viewer is
# WebGL, so "does it render" cannot be answered by reading HTML. Chrome runs
# with SwiftShader because headless has no GPU; that is slow but it exercises
# the same code path a real browser takes.
#
#   .\scripts\shoot.ps1 -Port 5057 -Paths "/", "/viewer"
param(
    [int]    $Port    = 5057,
    [string[]]$Paths  = @("/"),
    [string] $OutDir  = "shots",
    [int]    $Budget  = 12000,
    [string] $Size    = "1600,1000"
)

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { throw "Chrome not found at $chrome" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$jobs = @()
foreach ($path in $Paths) {
    # One name per view, so repeated runs overwrite rather than accumulate.
    $name = ($path -replace '^/', '' -replace '[/?&=]', '_' -replace '_+', '_').Trim('_')
    if (-not $name) { $name = "root" }
    $file = (Resolve-Path $OutDir).Path + "\$name.png"
    $url  = "http://127.0.0.1:$Port$path"

    # Each Chrome needs its own profile directory or the second one attaches
    # to the first and never writes a file.
    $profile = Join-Path $env:TEMP "shoot-$name-$PID"

    $jobs += Start-Job -ScriptBlock {
        param($chrome, $file, $url, $budget, $size, $profile)
        & $chrome --headless=new --use-angle=swiftshader --enable-unsafe-swiftshader `
            --hide-scrollbars --disable-extensions --no-first-run `
            --user-data-dir=$profile --window-size=$size `
            --virtual-time-budget=$budget --screenshot=$file $url 2>&1 | Out-Null
        [pscustomobject]@{ File = $file; Url = $url }
    } -ArgumentList $chrome, $file, $url, $Budget, $Size, $profile
}

Write-Host "Capturing $($jobs.Count) view(s)..."
$jobs | Wait-Job | Out-Null
foreach ($job in $jobs) {
    $r = Receive-Job $job
    $ok = Test-Path $r.File
    $kb = if ($ok) { "{0:N0} KB" -f ((Get-Item $r.File).Length / 1KB) } else { "MISSING" }
    "{0,-60} {1}" -f $r.Url, $kb
    Remove-Job $job
}
Get-ChildItem $env:TEMP -Filter "shoot-*-$PID" -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
