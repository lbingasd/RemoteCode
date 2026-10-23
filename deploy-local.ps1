#Requires -Version 5.1
<#
Home-machine Windows entrypoint; run as the ordinary developer account.
Process: find Python -> WinGet deps (Python/Node/Git) -> setup.py local
         (IDE/frpc -> runtime -> ACP adapters + prerequisite Codex/Claude
         detection -> settings/extension -> NSSM service / scheduled task -> doctor).
Required input: -Bundle <dir>, -Workspace <dir>, -AcceptLicense. Optional: -CN.
Only service registration prompts for UAC.
#>
[CmdletBinding()]
param(
    [ValidateSet('Install','Status','Uninstall')][string]$Action = 'Install',
    [string]$Root = "$env:USERPROFILE\RemoteCode",
    [string]$Bundle,
    [string]$Workspace,
    [string]$Cache,
    [string]$Lock,
    [string]$Mirror,
    [string]$Registry,
    [ValidateRange(1,16)][int]$Workers = 8,
    [switch]$CN,
    [switch]$SkipDeps,
    [switch]$Offline,
    [switch]$AcceptLicense,
    [switch]$NoAgents,
    [switch]$NoExtension,
    [switch]$NoService,
    [switch]$Headless,
    [switch]$QQBot,
    [switch]$SimpleDownload,
    [switch]$Purge,
    [string]$Vsix,
    [string]$VsixSha256,
    [string]$NssmPath,
    [string]$NssmSha256,
    [switch]$Help
)
$Cache = if ($Cache) { $Cache } else { Join-Path $PSScriptRoot 'downloads' }
$ErrorActionPreference = 'Stop'

function Refresh-DependencyPath {
    $Entries = @(
        [Environment]::GetEnvironmentVariable('Path','Machine'),
        [Environment]::GetEnvironmentVariable('Path','User'),
        $env:Path,
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:ProgramFiles\Python312",
        "$env:ProgramFiles\nodejs",
        "$env:LOCALAPPDATA\Programs\Git\cmd",
        "$env:ProgramFiles\Git\cmd",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps"
    )
    $env:Path = ($Entries | Where-Object { $_ }) -join ';'
}
function Find-Python {
    $Candidates = @($env:REMOTECODE_PYTHON, "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")
    $Candidates += @(Get-Command python.exe,python3.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    foreach ($Candidate in $Candidates) {
        if (-not $Candidate -or $Candidate -match '\\Microsoft\\WindowsApps\\' -or -not (Test-Path -LiteralPath $Candidate)) { continue }
        try {
            & $Candidate -c 'import sys; sys.exit(sys.version_info < (3,12))' 2>$null
            if ($LASTEXITCODE -eq 0) { return $Candidate }
        } catch { }
    }
    return $null
}
function Test-Node {
    $Node = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $Node) { return $false }
    & $Node.Source -e 'process.exit(parseInt(process.versions.node) < 22 ? 1 : 0)'
    return $LASTEXITCODE -eq 0
}
function Ensure-WinGet {
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) { return }
    Write-Host 'Bootstrapping WinGet using the official Microsoft.WinGet.Client module...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Scope CurrentUser -Force | Out-Null
    Install-Module -Name Microsoft.WinGet.Client -Repository PSGallery -Scope CurrentUser -Force -AllowClobber | Out-Null
    Import-Module Microsoft.WinGet.Client
    Repair-WinGetPackageManager -Force -Latest
    Refresh-DependencyPath
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw 'WinGet unavailable. Install Microsoft App Installer/Python/Node/Git, then rerun with -SkipDeps.' }
}
function Install-WinGetPackage([string]$Id) {
    Ensure-WinGet
    & winget.exe install --id $Id --exact --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    $Code = $LASTEXITCODE
    if ($Code -ne 0 -and $Code -ne 3010) { throw "WinGet failed for $Id ($Code). Rerun after fixing package-manager/network errors." }
    Refresh-DependencyPath
}
function ConvertTo-NativeArgument([string]$Value) {
    # Standard Windows CRT argv escaping, including quotes/trailing backslashes.
    # Avoid Windows PowerShell 5.1's legacy native array-to-command-line conversion.
    $Escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $Escaped = [regex]::Replace($Escaped, '(\\+)$', '$1$1')
    return '"' + $Escaped + '"'
}

try {
    if ($Help) {
        Write-Host '.\deploy-local.ps1 -Action Install -Bundle .\pc1 -Workspace D:\Projects -AcceptLicense [-CN]'
        Write-Host 'Options: -Root -Cache -Lock -Mirror https://your.mirror/{url} -Registry -Offline -SkipDeps'
        Write-Host '         -NoAgents -NoExtension -NoService -Headless -QQBot -Vsix -VsixSha256 -NssmPath -NssmSha256'
        Write-Host 'Uninstall: .\deploy-local.ps1 -Action Uninstall [-Purge]; -Purge also removes the download cache.'
        exit 0
    }
    if ($env:OS -ne 'Windows_NT') { throw 'Use deploy-local.sh on macOS/Linux.' }
    if ($Action -eq 'Install') {
        $Admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if ($Admin) { throw 'Run Install from ordinary developer PowerShell. The script prompts for UAC only for services.' }
        if (-not $AcceptLicense -or -not $Bundle -or -not $Workspace) { throw 'Install requires -Bundle, -Workspace and -AcceptLicense (Microsoft VS Code Server license).' }
        if (-not (Test-Path -LiteralPath (Join-Path $Bundle 'machine.json'))) { throw 'Bundle must contain machine.json and ca.pem exported by the gateway.' }
    }
    Refresh-DependencyPath
    $Python = Find-Python
    if ($Action -eq 'Install' -and -not $SkipDeps -and -not $Offline) {
        if (-not $Python) { Install-WinGetPackage 'Python.Python.3.12'; $Python = Find-Python }
        if (-not $NoAgents) {
            if (-not (Test-Node)) { Install-WinGetPackage 'OpenJS.NodeJS.LTS' }
            if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { Install-WinGetPackage 'Git.Git' }
            if (-not (Test-Node) -or -not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw 'Node.js 22+/Git installation incomplete. Reopen PowerShell and rerun.' }
        }
    }
    if (-not $Python) { throw 'Python 3.12+ required. Set REMOTECODE_PYTHON to its absolute python.exe path.' }
    $env:PYTHONUNBUFFERED = '1'
    $Arguments = @('-u', (Join-Path $PSScriptRoot 'scripts\setup.py'), 'local', $Action.ToLowerInvariant(), '--root', $Root, '--cache', $Cache, '--workers', "$Workers")
    $Values = @{
        '--bundle'=$Bundle; '--workspace'=$Workspace; '--lock'=$Lock; '--mirror'=$Mirror; '--registry'=$Registry
        '--vsix'=$Vsix; '--vsix-sha256'=$VsixSha256; '--nssm-path'=$NssmPath; '--nssm-sha256'=$NssmSha256
    }
    foreach ($Key in $Values.Keys) { if ($Values[$Key]) { $Arguments += @($Key, $Values[$Key]) } }
    $Flags = @{
        '--cn'=$CN; '--skip-deps'=$SkipDeps; '--offline'=$Offline; '--accept-license'=$AcceptLicense
        '--no-agents'=$NoAgents; '--no-extension'=$NoExtension; '--no-service'=$NoService
        '--headless'=$Headless; '--simple-download'=$SimpleDownload; '--purge'=$Purge
        '--qq-bot'=$QQBot
    }
    foreach ($Key in $Flags.Keys) { if ($Flags[$Key]) { $Arguments += $Key } }
    $Start = New-Object Diagnostics.ProcessStartInfo
    $Start.FileName = $Python
    $Start.Arguments = ($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' '
    $Start.WorkingDirectory = (Get-Location).ProviderPath
    $Start.UseShellExecute = $false
    $Process = [Diagnostics.Process]::Start($Start)
    $Process.WaitForExit()
    exit $Process.ExitCode
} catch {
    Write-Error $_
    exit 1
}
