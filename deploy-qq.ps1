#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('install','configure','pair','status','start','stop','restart','unbind','uninstall')]
    [string]$Action = 'install',
    [string]$Root = "$env:USERPROFILE\RemoteCode",
    [switch]$NoConfigure,
    [switch]$NoService
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use deploy-qq.sh on macOS/Linux.' }
$Candidates = @($env:REMOTECODE_PYTHON, "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")
$Candidates += @(Get-Command python.exe,python3.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
$Python = $null
foreach ($Candidate in $Candidates) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) { continue }
    & $Candidate -c 'import sys; sys.exit(sys.version_info < (3,12))' 2>$null
    if ($LASTEXITCODE -eq 0) { $Python = $Candidate; break }
}
if (-not $Python) { throw 'Python 3.12+ required; set REMOTECODE_PYTHON.' }
$Arguments = @('-u', (Join-Path $PSScriptRoot 'scripts\qq_bot.py'), $Action, '--root', $Root)
if ($NoConfigure) { $Arguments += '--no-configure' }
if ($NoService) { $Arguments += '--no-service' }
# Pass only paths/options. Credentials are read by Python getpass in this console.
& $Python @Arguments
exit $LASTEXITCODE
