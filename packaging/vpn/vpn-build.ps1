# 用系统自带的 csc.exe 编译 vpnd.exe 与 vpn-setup.exe（不引入新工具链）。
#
# 用法：powershell -ExecutionPolicy Bypass -File user\packaging\vpn\vpn-build.ps1
# 产物：user\packaging\vpn\out\{vpnd.exe, vpn-setup.exe, wintun.dll, LICENSE.txt}
param(
    [string]$OutDir = ""
)

if (-not $OutDir) {
    $OutDir = Join-Path $PSScriptRoot "out"
}

$ErrorCount = 0
$csc = $null
foreach ($candidate in @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe")) {
    if (Test-Path $candidate) { $csc = $candidate; break }
}
if (-not $csc) {
    Write-Error "找不到 .NET Framework 4.x 的 csc.exe"
    exit 2
}
$frameworkDir = Split-Path $csc -Parent

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$common = @(
    "/nologo", "/optimize+", "/target:exe", "/platform:x64",
    "/r:System.dll", "/r:System.Core.dll", "/r:System.ServiceProcess.dll",
    "/r:System.Web.Extensions.dll"
)

$vpndSources = @(
    (Join-Path $PSScriptRoot "Vpnd.cs"),
    (Join-Path $PSScriptRoot "Wintun.cs"),
    (Join-Path $PSScriptRoot "UdpTransport.cs"),
    (Join-Path $PSScriptRoot "Crypto.cs"),
    (Join-Path $PSScriptRoot "Ipc.cs"),
    (Join-Path $PSScriptRoot "ProcessUtil.cs")
)

Write-Host "==> vpnd.exe"
& $csc @common "/out:$OutDir\vpnd.exe" @vpndSources
if ($LASTEXITCODE -ne 0) { $ErrorCount++ }

Write-Host "==> vpn-setup.exe"
& $csc @common "/win32manifest:$(Join-Path $PSScriptRoot 'VpnSetup.manifest')" `
    "/out:$OutDir\vpn-setup.exe" `
    (Join-Path $PSScriptRoot "VpnSetup.cs") `
    (Join-Path $PSScriptRoot "ProcessUtil.cs")
if ($LASTEXITCODE -ne 0) { $ErrorCount++ }

Copy-Item (Join-Path $PSScriptRoot "bin\wintun.dll") $OutDir -Force
Copy-Item (Join-Path $PSScriptRoot "bin\LICENSE.txt") $OutDir -Force

if ($ErrorCount -ne 0) {
    Write-Error "编译失败（$ErrorCount 个目标）"
    exit 1
}
Write-Host "编译完成：$OutDir"
Get-ChildItem $OutDir | Select-Object Name, Length | Format-Table -AutoSize
exit 0
