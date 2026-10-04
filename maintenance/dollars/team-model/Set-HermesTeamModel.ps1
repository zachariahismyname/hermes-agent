param(
    [string]$Model,
    [ValidateSet('none','minimal','low','medium','high','xhigh','max','ultra')][string]$Thinking,
    [ValidateSet('normal','fast','ultrafast')][string]$Speed,
    [switch]$Apply,
    [switch]$HermesOnly,
    [string]$ApiBase = 'http://127.0.0.1:3100',
    [Alias('h')][switch]$Help,
    [string]$RuntimeManifest = (Join-Path $PSScriptRoot '../../../../releases/tooling-runtime.json')
)
$ErrorActionPreference = 'Stop'
$manifest = Get-Content -LiteralPath $RuntimeManifest -Raw | ConvertFrom-Json
if ($manifest.schema -ne 1 -or $manifest.mode -notin @('development','approved-release')) { throw 'Invalid runtime manifest.' }
$runtime = $manifest.python
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) { throw 'Manifest Python does not exist.' }
$moduleRoot = & $runtime -c 'import pathlib,hermes_cli; print(pathlib.Path(hermes_cli.__file__).resolve().parent.parent)'
if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($moduleRoot.Trim()) -ne [IO.Path]::GetFullPath($manifest.module_root)) { throw 'Runtime source does not match manifest.' }
if ($manifest.mode -eq 'development') {
    $staging = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../../staging/')).TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
    $target = [IO.Path]::GetFullPath($manifest.hermes_root)
    if (-not $target.StartsWith($staging, [StringComparison]::OrdinalIgnoreCase)) { throw 'Development home must be inside staging.' }
    if ($Apply -and -not $HermesOnly) { throw 'Development apply requires -HermesOnly to avoid writing to Paperclip.' }
}
$arguments = @('-X','utf8',(Join-Path $PSScriptRoot 'set_hermes_team_model.py'))
$previousHome = $env:HERMES_HOME
try {
    $env:HERMES_HOME = $manifest.hermes_root
    if ((-not $Model -and -not $Thinking -and -not $Speed -and -not $Apply) -or $Help) {
        & $runtime @arguments --help
        if ($LASTEXITCODE -ne 0) { throw 'Unable to display model settings help.' }
        return
    }
    if (-not $Model -or -not $Thinking -or -not $Speed) {
        & $runtime @arguments --help
        throw 'Supply -Model, -Thinking, and -Speed together. Add -Apply only when ready to save.'
    }
    $arguments += @('--model',$Model,'--thinking',$Thinking,'--speed',$Speed,'--api-base',$ApiBase)
    if ($Apply) { $arguments += '--apply' }
    if ($HermesOnly) { $arguments += '--hermes-only' }
    & $runtime @arguments
    if ($LASTEXITCODE -ne 0) { throw "Model settings script failed (exit $LASTEXITCODE)." }
} finally {
    $env:HERMES_HOME = $previousHome
}
