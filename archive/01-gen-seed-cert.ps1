# 01-gen-seed-cert.ps1 —— 生成多 SAN 自签"种子证书"
# 用途：把多域名 SAN 喂进 fnOS 证书库（cert.upload），随后由 fnOS 的 ACME 引擎签真证书。
# 兼容 Windows PowerShell 5.1（用 CngKey.Pkcs8PrivateBlob 导出私钥）。
#
# 用法：
#   .\01-gen-seed-cert.ps1 -Domains "a.example.com","*.example.com","b.example.com"
#   .\01-gen-seed-cert.ps1 -Domains "a.com","b.com" -Cn "a.com" -OutDir "."

param(
    [Parameter(Mandatory = $true)][string[]]$Domains,
    [string]$Cn = "",
    [string]$OutDir = "."
)

if (-not $Cn) { $Cn = $Domains[0] }

Add-Type -AssemblyName System.Security

# 1. 构造 SAN 扩展
$san = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
foreach ($d in $Domains) { $san.AddDnsName($d) }

# 2. 自签证书（1 年，RSA 2048 / SHA256）
$req = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    "CN=$Cn",
    [System.Security.Cryptography.RSA]::Create(2048),
    [System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
)
$req.CertificateExtensions.Add($san.Build())
$cert = $req.CreateSelfSigned([DateTimeOffset]::Now.AddDays(-1), [DateTimeOffset]::Now.AddYears(1))

# 3. 导出 PEM（证书 + PKCS#8 私钥）
$rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert)
$certPem = "-----BEGIN CERTIFICATE-----`n" +
    [Convert]::ToBase64String($cert.RawData, [System.Base64FormattingOptions]::InsertLineBreaks) +
    "`n-----END CERTIFICATE-----"
$keyBytes = $rsa.Key.Export([System.Security.Cryptography.CngKeyBlobFormat]::Pkcs8PrivateBlob)
$keyPem = "-----BEGIN PRIVATE KEY-----`n" +
    [Convert]::ToBase64String($keyBytes, [System.Base64FormattingOptions]::InsertLineBreaks) +
    "`n-----END PRIVATE KEY-----"

Set-Content -Path (Join-Path $OutDir "seed.crt") -Value $certPem -NoNewline
Set-Content -Path (Join-Path $OutDir "seed.key") -Value $keyPem -NoNewline

# 4. 输出 base64（供 02-fnos-cert-api.js 使用）
$certB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($certPem))
$keyB64  = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($keyPem))
Set-Content -Path (Join-Path $OutDir "seed.base64.txt") -Value "CERT_B64=$certB64`nKEY_B64=$keyB64"

Write-Host "已生成 seed.crt / seed.key / seed.base64.txt"
Write-Host "SAN: $($Domains -join ', ')"
Write-Host ""
Write-Host "CERT_B64=$certB64"
Write-Host ""
Write-Host "KEY_B64=$keyB64"
