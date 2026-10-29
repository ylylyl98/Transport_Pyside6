[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $PSScriptRoot '..\tmp\inno-setup-7.1.1')
)

$ErrorActionPreference = 'Stop'
$toolVersion = '7.1.1'
$downloadHash = '8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0'
$downloadUrl = 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe'
$toolDirectory = [IO.Path]::GetFullPath($Destination)
$compiler = Join-Path $toolDirectory 'ISCC.exe'
if (Test-Path -LiteralPath $compiler) {
    $installedVersion = (& $compiler --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $installedVersion -ne $toolVersion) {
        throw "Expected Inno Setup $toolVersion, found $installedVersion at $compiler"
    }
    Write-Output $compiler
    exit 0
}

$downloadDirectory = Join-Path (Split-Path $toolDirectory -Parent) 'inno-download'
New-Item -ItemType Directory -Force -Path $downloadDirectory | Out-Null
$downloadFile = Join-Path $downloadDirectory 'innosetup-7.1.1-x64.exe'
if (-not (Test-Path -LiteralPath $downloadFile)) {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $downloadFile
}
$actualHash = (Get-FileHash -LiteralPath $downloadFile -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $downloadHash) {
    throw "Inno Setup download checksum mismatch; remove the cached download and retry: $downloadFile"
}
$signature = Get-AuthenticodeSignature -LiteralPath $downloadFile
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Pyrsys B\.V\.') {
    throw "Inno Setup download does not have the expected valid publisher signature: $($signature.Status)"
}

# The compiler is a development dependency, never part of the Transport installer.
# Use a per-user, task-local installation; no global PATH or file associations.
$arguments = @('/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
               '/SP-', '/NOICONS', '/TASKS=', "/DIR=`"$toolDirectory`"",
               "/LOG=`"$(Join-Path $downloadDirectory 'install.log')`"")
$process = Start-Process -FilePath $downloadFile -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $compiler)) {
    throw "Inno Setup installation failed ($($process.ExitCode)); inspect $downloadDirectory\install.log"
}
$installedVersion = (& $compiler --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $installedVersion -ne $toolVersion) {
    throw "Expected Inno Setup $toolVersion, found $installedVersion"
}
Write-Output $compiler
