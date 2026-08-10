param(
    [string] $HostAddress = "127.0.0.1",
    [int] $Port = 8788,
    # Keep this script ASCII-safe because Windows PowerShell 5 reads a UTF-8
    # file without BOM as the legacy system code page.
    [string] $StorageRoot = (
        "E:\wangyang\Videos\" +
        (-join @([char]0x77ED, [char]0x5267, [char]0x8F93, [char]0x51FA))
    ),
    [string] $PublicUrl = "https://upload.andymori.uk",
    [string] $AllowedOrigins = ""
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv-ocr\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    # A Git worktree intentionally does not duplicate the multi-gigabyte OCR
    # environment. Reuse the sibling checkout runtime when it exists.
    $sharedPython = Join-Path (Split-Path $PSScriptRoot -Parent) "video-dedup-local\.venv-ocr\Scripts\python.exe"
    if (Test-Path -LiteralPath $sharedPython) {
        $python = $sharedPython
        $pythonPrefix = @()
    } else {
        $python = "py"
        $pythonPrefix = @("-3.12")
    }
} else {
    $pythonPrefix = @()
}
$env:VIDEO_GATEWAY_STORAGE_ROOT = $StorageRoot
$env:VIDEO_GATEWAY_PUBLIC_URL = $PublicUrl
$env:VIDEO_GATEWAY_ALLOWED_ORIGINS = $AllowedOrigins
$localAsrPython = Join-Path $PSScriptRoot ".venv-asr\Scripts\python.exe"
$sharedAsrPython = Join-Path (Split-Path $PSScriptRoot -Parent) "video-dedup-local\.venv-asr\Scripts\python.exe"
if (Test-Path -LiteralPath $localAsrPython) {
    $env:VIDEO_TOOL_ASR_PYTHON = $localAsrPython
} elseif (Test-Path -LiteralPath $sharedAsrPython) {
    $env:VIDEO_TOOL_ASR_PYTHON = $sharedAsrPython
}
$serviceRoot = Join-Path $StorageRoot ".video-service"
New-Item -ItemType Directory -Path $serviceRoot -Force | Out-Null
$serviceLog = Join-Path $serviceRoot "gateway-service.log"
$errorLog = Join-Path $serviceRoot "gateway-service.error.log"
$lifecycleLog = Join-Path $serviceRoot "gateway-lifecycle.log"
foreach ($path in @($serviceLog, $errorLog, $lifecycleLog)) {
    if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 10MB) {
        Move-Item -LiteralPath $path -Destination "$path.1" -Force
    }
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::AppendAllText(
    $lifecycleLog,
    "[$(Get-Date -Format o)] Starting gateway on $HostAddress`:$Port, public URL $PublicUrl`r`n",
    $utf8
)
$arguments = @($pythonPrefix) + @(
    "-u", "-m", "web_gateway.cli", "serve",
    "--host", $HostAddress,
    "--port", [string]$Port
)
$gateway = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $serviceLog -RedirectStandardError $errorLog
$exitCode = $gateway.ExitCode
[System.IO.File]::AppendAllText(
    $lifecycleLog,
    "[$(Get-Date -Format o)] Gateway exited with code $exitCode`r`n",
    $utf8
)
exit $exitCode
