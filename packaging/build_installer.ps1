# Build the CrMCDownload Windows installer.
#
# The setup is a self-contained C# executable compiled with the .NET Framework
# compiler that ships with Windows (csc.exe) - no Inno Setup / NSIS / WiX needed.
# The application payload is embedded as a managed resource and unpacked
# straight into the target directory, so the setup never extracts anything into
# %TEMP%.
#
# Two binaries are produced from packaging\installer\Setup.cs:
#   uninstaller.exe  compiled with /define:UNINSTALLER (no payload, ~30 KB)
#   setup.exe        embeds payload.zip + uninstaller.exe as resources
#
# Usage:  powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1
#
# Requires dist\CrMCDownload\CrMCDownload.exe (pyinstaller CrMCDownload.spec).
param(
    [string]$Dist = ''
)

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$root = Split-Path -Parent $here

function Get-AppVersion {
    $initPy = Join-Path $root 'offcraft\__init__.py'
    $text = [System.IO.File]::ReadAllText($initPy)
    $m = [regex]::Match($text, '__version__\s*=\s*"([0-9A-Za-z\.\-]+)"')
    if (-not $m.Success) { throw "cannot read __version__ from $initPy" }
    return $m.Groups[1].Value
}

$Version = Get-AppVersion
$p = $Version.Split('.')
while ($p.Length -lt 4) { $p += '0' }
$Version4 = ($p[0..3] -join '.')

if ($Dist -eq '') { $Dist = Join-Path $root 'dist\CrMCDownload' }
$distExe = Join-Path $Dist 'CrMCDownload.exe'
if (-not (Test-Path $distExe)) {
    throw "missing $distExe - build it first with: pyinstaller CrMCDownload.spec --noconfirm --clean"
}
$distVer = (Get-Item $distExe).VersionInfo.FileVersion
if ($distVer -ne $Version) {
    throw "version mismatch: app exe is $distVer but offcraft.__version__ is $Version (rebuild the exe)"
}

$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "csc.exe not found at $csc (.NET Framework 4.x is required)" }

$icon = Join-Path $root 'assets\icon.ico'
$manifest = Join-Path $here 'installer\app.manifest'
$setupSrc = Join-Path $here 'installer\Setup.cs'
$buildDir = Join-Path $root 'build\installer'
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$payload = Join-Path $buildDir 'payload.zip'

# VPN components: rebuild on every package so vpnd.exe / vpn-setup.exe stay in sync
# with the app sources, then copy them next to the main exe. PyInstaller 6 onedir
# keeps auxiliary files under "_internal", so that is the real payload content dir.
Write-Host '[0/4] building VPN components'
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'vpn\vpn-build.ps1')
if ($LASTEXITCODE -ne 0) { throw 'vpn build failed' }
$vpnOut = Join-Path $here 'vpn\out'
$contentDir = Join-Path -Path $Dist -ChildPath '_internal'
if (-not (Test-Path -Path $contentDir)) { $contentDir = $Dist }
foreach ($name in @('vpnd.exe', 'vpn-setup.exe', 'wintun.dll', 'LICENSE.txt')) {
    $src = Join-Path $vpnOut $name
    if (-not (Test-Path $src)) { throw "missing $src (vpn-build.ps1 failed?)" }
    Copy-Item $src (Join-Path $contentDir $name) -Force
}

Write-Host "[1/4] packing payload from $Dist"
& python (Join-Path $here 'make_payload.py') $Dist $payload
if ($LASTEXITCODE -ne 0) { throw 'payload build failed' }

Write-Host "[2/4] generating version metadata ($Version4)"
$bi = @"
using System.Reflection;
[assembly: AssemblyTitle("CrMCDownload Setup")]
[assembly: AssemblyDescription("CrMCDownload Setup")]
[assembly: AssemblyCompany("Cr")]
[assembly: AssemblyProduct("CrMCDownload")]
[assembly: AssemblyCopyright("Copyright (C) 2026 Cr")]
[assembly: AssemblyVersion("$Version4")]
[assembly: AssemblyFileVersion("$Version4")]
namespace CrMcSetup
{
    internal static class BuildInfo
    {
        public const string Name = "CrMCDownload";
        public const string Publisher = "Cr";
        public const string Version = "$Version";
    }
}
"@
$biPath = Join-Path $buildDir 'BuildInfo.cs'
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($biPath, $bi, $utf8Bom)

$cscCommon = @(
    '/nologo', '/target:winexe', '/platform:anycpu', '/optimize+', '/codepage:65001',
    ('/win32icon:' + $icon),
    ('/win32manifest:' + $manifest),
    '/r:System.dll', '/r:System.Core.dll', '/r:System.Drawing.dll',
    '/r:System.Windows.Forms.dll', '/r:System.IO.Compression.dll',
    '/r:System.IO.Compression.FileSystem.dll'
)

Write-Host '[3/4] compiling uninstaller.exe'
$unExe = Join-Path $buildDir 'uninstaller.exe'
& $csc ($cscCommon + @('/define:UNINSTALLER', ('/out:' + $unExe), $setupSrc, $biPath))
if ($LASTEXITCODE -ne 0) { throw "csc (uninstaller) failed with exit code $LASTEXITCODE" }

Write-Host '[4/4] compiling setup.exe'
$outDir = Join-Path $root 'dist\installer'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$outExe = Join-Path $outDir ("CrMCDownload-$Version-win64-setup.exe")
& $csc ($cscCommon + @(
    ('/out:' + $outExe),
    ('/resource:' + $payload + ',payload.zip'),
    ('/resource:' + $unExe + ',uninstaller.exe'),
    $setupSrc,
    $biPath
))
if ($LASTEXITCODE -ne 0) { throw "csc (setup) failed with exit code $LASTEXITCODE" }
if (-not (Test-Path $outExe)) { throw 'csc reported success but no output file' }

$info = (Get-Item $outExe).VersionInfo
$hash = (Get-FileHash $outExe -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $outExe).Length

Write-Host ''
Write-Host 'installer ready:'
Write-Host ("  file    : " + $outExe)
Write-Host ("  size    : " + $size + " bytes")
Write-Host ("  sha256  : " + $hash)
Write-Host ("  FileVer : " + $info.FileVersion)
Write-Host ("  ProdVer : " + $info.ProductVersion)
Write-Host ("  Company : " + $info.CompanyName)
Write-Host ("  Payload : " + (Get-Item $payload).Length + " bytes (embedded as payload.zip)")
Write-Host ("  Uninst  : " + (Get-Item $unExe).Length + " bytes (embedded as uninstaller.exe)")

Write-Host ''
Write-Host 'NOTE: do NOT double-click this setup from the project workspace folder.'
Write-Host '      Processes started from this workspace are denied writes to C:, so the setup'
Write-Host '      stops right away (rc=1, no window). Copy it to your Downloads folder first:'
Write-Host ('      ' + $env:USERPROFILE + '\Downloads\' + (Split-Path $outExe -Leaf))
Write-Host '      (The setup itself never uses %TEMP%, so any normal folder works.)'
