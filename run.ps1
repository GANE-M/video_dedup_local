param(
    [Parameter(Mandatory = $true, Position = 0)] [string] $InputPath,
    [Parameter(Mandatory = $true, Position = 1)] [string] $OutputPath,
    [ValidateSet("light", "medium", "strong")] [string] $Preset = "medium",
    [int] $Seed = 2026,
    [string] $Config = ""
)

$ErrorActionPreference = "Stop"
$tool = Join-Path $PSScriptRoot "video_dedup.py"
$arguments = @($tool, $InputPath, $OutputPath, "--preset", $Preset, "--seed", $Seed)
if ($Config) {
    $arguments += @("--config", $Config)
}
python @arguments
exit $LASTEXITCODE
