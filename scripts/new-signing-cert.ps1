# 一次性生成 winget 源签名证书（自签代码签名证书），并输出后续配置所需的全部信息。
#
# 需在 Windows 上以管理员 PowerShell 运行。生成内容:
#   signing.pfx      —— 私钥证书，Base64 后放入 GitHub Secrets 的 SIGNING_PFX
#   winget-subset.cer —— 公钥证书，导入每台使用该源的机器 TrustedPeople
#
# 用法: .\scripts\new-signing-cert.ps1 [-OutDir .]

[CmdletBinding()]
param(
    [string]$Subject = 'CN=winget-subset Source',
    [int]$Years = 10,
    [string]$OutDir = '.'
)

$ErrorActionPreference = 'Stop'

$password = ConvertTo-SecureString -String ([Guid]::NewGuid().ToString('N') + '!Aa1') -AsPlainText -Force

$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject $Subject `
    -KeyUsage DigitalSignature `
    -FriendlyName 'winget-subset source signing' `
    -CertStoreLocation 'Cert:\CurrentUser\My' `
    -NotAfter (Get-Date).AddYears($Years) `
    -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3')

$pfxPath = Join-Path $OutDir 'signing.pfx'
$cerPath = Join-Path $OutDir 'winget-subset.cer'
Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $password | Out-Null
Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null

$pfxB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($pfxPath))
Set-Content (Join-Path $OutDir 'SIGNING_PFX.txt') $pfxB64
Set-Content (Join-Path $OutDir 'SIGNING_PFX_PASSWORD.txt') ([Net.NetworkCredential]::new('', $password).Password)

@"

==================== 后续步骤 ====================
1. GitHub Secrets:
     SIGNING_PFX          <- SIGNING_PFX.txt 内容
     SIGNING_PFX_PASSWORD <- SIGNING_PFX_PASSWORD.txt 内容
2. config.json 的 publisherDn 必须为:
     "$Subject"
   （若与当前值不同，请同步修改）
3. 每台使用本源的 Windows 机器执行一次（管理员，非管理员则仅当前用户生效）:
     .\scripts\install-source-cert.ps1 -SourceUrl https://winget.你的域名.com
   （该脚本从已部署的 source.msix 提取证书并导入 Root 与 TrustedPeople，无需拷贝 .cer 文件）
==================================================
"@ | Write-Host
