param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ToolArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$work = Join-Path $root "work"
New-Item -ItemType Directory -Force -Path $work | Out-Null

docker run --rm -it `
    -v "${work}:/work" `
    -e OPENAI_API_KEY `
    -e OPENAI_BASE_URL `
    -e OPENAI_MODEL `
    video-dedup-local:latest `
    @ToolArgs
