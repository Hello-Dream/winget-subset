# 一键信任 winget PreIndexed 源签名证书
# 下载 source.msix -> 提取签名证书 -> 导入 Root 与 TrustedPeople 信任存储
# 用法:
#   .\scripts\install-source-cert.ps1 -SourceUrl https://winget.你的域名.com
#   .\scripts\install-source-cert.ps1 -LocalMsix .\source.msix        # 已有本地 msix 时免下载
# 适用场景: winget source add 报 0x8a15003f「源数据已损坏或被篡改」—— 自签证书未被本机信任
# 管理员运行导入 LocalMachine（所有用户生效）; 非管理员自动回退 CurrentUser（仅当前用户）; -WhatIf 可预演

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$SourceUrl,
    [string]$LocalMsix
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------- 1. 获取 source.msix ----------
$msixPath = $LocalMsix
$isTemp = $false
if (-not $msixPath) {
    if (-not $SourceUrl) { throw '必须提供 -SourceUrl（源地址，如 https://winget.你的域名.com）或 -LocalMsix' }
    $msixPath = Join-Path ([IO.Path]::GetTempPath()) ('winget-subset-' + [Guid]::NewGuid().ToString('N') + '.msix')
    $ProgressPreference = 'SilentlyContinue'
    Write-Host "正在下载 $($SourceUrl.TrimEnd('/'))/source.msix ..."
    Invoke-WebRequest -Uri ($SourceUrl.TrimEnd('/') + '/source.msix') -OutFile $msixPath
    $isTemp = $true
}
if (-not (Test-Path -LiteralPath $msixPath)) { throw "文件不存在: $msixPath" }

# ---------- 2. 提取签名证书 ----------
$sig = Get-AuthenticodeSignature $msixPath
$cert = $sig.SignerCertificate
if (-not $cert) {
    if ($isTemp) { Remove-Item -LiteralPath $msixPath -Force }
    throw '未在 msix 中找到签名证书（可能是 -SkipSigning 的未签名产物），无法建立信任'
}
Write-Host "签名证书: $($cert.Subject)"
Write-Host "有效期: $($cert.NotBefore.ToString('yyyy-MM-dd')) ~ $($cert.NotAfter.ToString('yyyy-MM-dd'))"

# ---------- 3. 导入信任存储（Root 覆盖链校验，TrustedPeople 覆盖 winget 的 MSIX 包签名校验）----------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$location = if ($isAdmin) { 'LocalMachine' } else { 'CurrentUser' }
foreach ($storeName in @('Root', 'TrustedPeople')) {
    $store = [Security.Cryptography.X509Certificates.X509Store]::new($storeName, $location)
    $store.Open('ReadWrite')
    try {
        $exists = $store.Certificates | Where-Object Thumbprint -eq $cert.Thumbprint
        if ($exists) {
            Write-Host "证书已在 $location\$storeName，跳过"
        } elseif ($PSCmdlet.ShouldProcess("$location\$storeName", "导入源签名证书 $($cert.Thumbprint)")) {
            $store.Add($cert)
            Write-Host "已导入 $location\$storeName"
        }
    } finally {
        $store.Close()
    }
}
if (-not $isAdmin) { Write-Warning '非管理员，仅导入当前用户存储；建议以管理员运行一次以对所有用户生效' }

# ---------- 4. 校验 ----------
$sig2 = Get-AuthenticodeSignature $msixPath
if ($sig2.Status -eq 'Valid') {
    Write-Host '校验通过: 签名链已受信任，重新执行 winget source add 即可'
} else {
    Write-Warning "签名状态仍为 $($sig2.Status)。若本次是 -WhatIf 预演或证书仅导入 TrustedPeople，均属预期; 直接重新执行 winget source add 验证"
}

if ($isTemp) { Remove-Item -LiteralPath $msixPath -Force }
