# 构建 winget PreIndexed 源包: manifests/ + source-tpl/ -> dist/source.msix (+ dist/pages/)
# 用法: .\scripts\build_source.ps1 [-SkipSigning]

[CmdletBinding()]
param(
    [switch]$SkipSigning
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path $PSScriptRoot -Parent
$config = Get-Content (Join-Path $repoRoot 'config.json') -Raw | ConvertFrom-Json
$distDir = Join-Path $repoRoot 'dist'
$stageDir = Join-Path $distDir 'stage'
$pagesDir = Join-Path $distDir 'pages'
$flatDir = Join-Path $distDir 'flat-manifests'

function Get-NuGetSdkTools {
    # 从微软官方 NuGet 包获取打包工具；优先落盘 WINGET_SDK_TOOLS_DIR（供 CI 跨运行缓存），否则临时目录复用
    $workDir = if ($env:WINGET_SDK_TOOLS_DIR) { $env:WINGET_SDK_TOOLS_DIR }
    else { Join-Path ([IO.Path]::GetTempPath()) 'winget-subset-sdktools' }
    $cached = Get-ChildItem $workDir -Recurse -Filter 'makeappx.exe' -ErrorAction SilentlyContinue |
        Where-Object FullName -match '\\x64\\' | Select-Object -First 1
    if ($cached) { return $cached.Directory.FullName }

    Write-Warning '本机未找到打包工具，正在从微软官方 NuGet 获取（约数十 MB，仅首次）...'
    $versions = (Invoke-RestMethod 'https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/index.json').versions |
        Where-Object { $_ -notmatch '-' } | Sort-Object { [version]$_ }
    $version = $versions[-1]
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    $nupkg = Join-Path $workDir 'tools.zip'
    Invoke-WebRequest `
        -Uri "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/$version/microsoft.windows.sdk.buildtools.$version.nupkg" `
        -OutFile $nupkg
    # 系统自带 tar(bsdtar) 直接解 zip，比 Expand-Archive 快数倍；异常时回退
    tar -xf $nupkg -C $workDir
    if ($LASTEXITCODE -ne 0) { Expand-Archive $nupkg $workDir -Force }
    $exe = Get-ChildItem $workDir -Recurse -Filter 'makeappx.exe' |
        Where-Object FullName -match '\\x64\\' | Select-Object -First 1
    if (-not $exe) { throw 'NuGet 包内未找到 x64 打包工具' }
    Write-Host "已就绪 SDK Build Tools $version"
    return $exe.Directory.FullName
}

function Find-SdkTool([string]$ToolName) {
    # 三级回退: WINGET_SDK_TOOLS_DIR -> 本机 Windows Kits -> 官方 NuGet 包兜底
    if ($env:WINGET_SDK_TOOLS_DIR) {
        $direct = Join-Path $env:WINGET_SDK_TOOLS_DIR "$ToolName.exe"
        if (Test-Path $direct) { return $direct }
    }

    $kitTool = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' -Recurse -Filter "$ToolName.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.Directory.Name -match '^\d+\.\d+' -and $_.FullName -match '\\x64\\' } |
        Sort-Object { [version]$_.Directory.Name } |
        Select-Object -Last 1
    if ($kitTool) { return $kitTool.FullName }

    $toolsDir = Get-NuGetSdkTools
    $fallback = Join-Path $toolsDir "$ToolName.exe"
    if (Test-Path $fallback) { return $fallback }
    throw "SDK 工具未找到: $ToolName"
}

# ---------- 1. 合并多文件清单为 singleton ----------
# winget Microsoft.PreIndexed.Package 源的索引 pathpart 仅指向一个文件，
# 不会自动拉取同目录的 installer/locale 伴随文件。合并后 winget 只需拉一个文件。
python (Join-Path $repoRoot 'scripts\flatten_manifests.py') `
    --input (Join-Path $repoRoot 'manifests') `
    --output $flatDir
if ($LASTEXITCODE -ne 0) { throw '清单合并为 singleton 失败' }

# 安全红线: 代理前缀注入 flatDir 副本，仓库内 manifests 始终保持微软原始地址，域名不进 git
# 必须在构建索引前注入，否则 index.db 的 hash 与部署文件不匹配导致 0x8a15003f
if ($env:WINGET_PROXY_PREFIX) {
    python (Join-Path $repoRoot 'scripts\sync_manifests.py') `
        --rewrite-dir $flatDir `
        --prefix $env:WINGET_PROXY_PREFIX
    if ($LASTEXITCODE -ne 0) { throw 'InstallerUrl 代理前缀注入失败' }
}

# ---------- 2. 构建索引（从合并后的清单构建，确保 hash 一致）----------
Remove-Item -Recurse -Force $stageDir -ErrorAction SilentlyContinue
python (Join-Path $repoRoot 'scripts\build_index.py') `
    --manifests $flatDir `
    --output (Join-Path $stageDir 'Public\index.db')
if ($LASTEXITCODE -ne 0) { throw 'index.db 构建失败' }

# ---------- 3. 组装包内容 ----------
# _headers 仅用于 Pages 部署（客户端缓存策略），排除在 msix 之外
Copy-Item (Join-Path $repoRoot 'source-tpl\*') $stageDir -Recurse -Force -Exclude '_headers'

# 版本号由时间戳派生，保证每次构建单调递增触发客户端更新
$epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$pkgVersion = '1.{0}.{1}.0' -f ($epoch -shr 16), ($epoch -band 0xFFFF)
$manifestPath = Join-Path $stageDir 'AppxManifest.xml'
$xml = [xml](Get-Content $manifestPath -Raw)
$xml.Package.Identity.Version = $pkgVersion
$xml.Package.Identity.Publisher = $config.publisherDn
$xml.Save($manifestPath)
Write-Host "包版本: $pkgVersion | Publisher: $($config.publisherDn)"

# ---------- 4. 打包与签名 ----------
$makeAppx = Find-SdkTool 'makeappx'
& $makeAppx pack /d $stageDir /p (Join-Path $distDir 'source.msix') /o
if ($LASTEXITCODE -ne 0) { throw 'makeappx 打包失败' }

if ($SkipSigning) {
    Write-Warning '已跳过签名 —— 未签名的源包客户端会拒绝加载，仅用于本地调试打包流程'
} elseif ($env:SIGNING_PFX) {
    # Secret 从网页粘贴常带入尾部换行，不去除会导致 PFX 解密失败
    $pfxPassword = ('' + $env:SIGNING_PFX_PASSWORD).Trim()
    $pfxPath = Join-Path $distDir 'signing.pfx'
    [IO.File]::WriteAllBytes($pfxPath, [Convert]::FromBase64String($env:SIGNING_PFX))
    & (Find-SdkTool 'signtool') sign /fd SHA256 /td SHA256 /tr $config.timestampServer `
        /f $pfxPath /p $pfxPassword (Join-Path $distDir 'source.msix')
    if ($LASTEXITCODE -ne 0) { throw 'signtool 签名失败' }
    Remove-Item $pfxPath -Force
    Write-Host 'source.msix 签名完成'
} else {
    throw '缺少 SIGNING_PFX 环境变量。请先运行 scripts/new-signing-cert.ps1 并配置 Secrets，或本地调试加 -SkipSigning'
}

# ---------- 5. 组装 Pages 部署目录 ----------
Remove-Item -Recurse -Force $pagesDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $pagesDir | Out-Null
Copy-Item (Join-Path $distDir 'source.msix') $pagesDir
Copy-Item $flatDir (Join-Path $pagesDir 'manifests') -Recurse
Copy-Item (Join-Path $repoRoot 'source-tpl\_headers') $pagesDir

# ---------- 6. 离线校验: 索引与清单一致性（失败即停，避免把坏源部署上线）----------
python (Join-Path $repoRoot 'scripts\validate_manifests.py') `
    --manifests $flatDir `
    --stage (Join-Path $stageDir 'Public\index.db') `
    --pages $pagesDir
if ($LASTEXITCODE -ne 0) { throw '清单离线校验未通过，终止构建' }

Write-Host "`n构建完成:"
Get-ChildItem $pagesDir | ForEach-Object { Write-Host ("  " + $_.Name) }
