#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('install','start','restart','stop','remove')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Pythonw
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $Root).Path
$Config = Get-Content -LiteralPath (Join-Path $Root 'local.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Config.id -notmatch '^[a-z][a-z0-9-]{0,24}$') { throw 'Invalid machine ID' }
$Name = 'remotecode-qq-' + $Config.id
$LegacyPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$LegacyArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Root 'qq\run-qq.ps1') + '"'
$Runner = Join-Path $Root 'qq\runtime\windows_qq.py'
# All values are paths; quoting follows Windows argv rules and contains no credentials.
$Arguments = '"' + $Runner + '" --root "' + $Root.TrimEnd('\') + '"'

function Assert-QqTaskOwner($Task) {
    if (-not $Task) { return }
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) { throw 'QQ task ownership mismatch' }
    $Legacy = $Actions[0].Execute -eq $LegacyPowerShell -and $Actions[0].Arguments -eq $LegacyArguments -and -not $Actions[0].WorkingDirectory
    $Current = $Actions[0].Execute -eq $Pythonw -and $Actions[0].Arguments -eq $Arguments -and $Actions[0].WorkingDirectory -eq $Root
    if (-not ($Legacy -or $Current)) { throw 'QQ task ownership mismatch' }
    $TaskUser = $Task.Principal.UserId
    if ($TaskUser -notmatch '^S-1-') {
        if ($TaskUser -notmatch '[\\@]') { $TaskUser = "$env:COMPUTERNAME\$TaskUser" }
        $TaskUser = (New-Object Security.Principal.NTAccount($TaskUser)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($TaskUser -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { throw 'QQ task belongs to another user' }
}
function Get-QqTaskSettings {
    New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden
}
function Get-QqRecoveryTrigger {
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
}

$Old = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
Assert-QqTaskOwner $Old
if ($Action -in @('install','start','restart')) {
    if (-not (Test-Path -LiteralPath $Pythonw) -or -not (Test-Path -LiteralPath $Runner)) { throw 'QQ supervisor missing; rerun deploy-qq.ps1 -Action install -NoConfigure' }
}
$Changed = $Old -and ($Old.Actions[0].Execute -ne $Pythonw -or $Old.Actions[0].Arguments -ne $Arguments)
if ($Old -and ($Changed -or $Action -in @('stop','restart','remove'))) {
    # Disable the periodic trigger before stopping so deliberate stops stay stopped.
    Disable-ScheduledTask -TaskName $Name | Out-Null
    Stop-ScheduledTask -TaskName $Name
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        if ((Get-ScheduledTask -TaskName $Name).State -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
    }
    if ((Get-ScheduledTask -TaskName $Name).State -eq 'Running') { throw 'QQ task did not stop' }
}
if ($Action -eq 'remove') {
    if ($Old) { Unregister-ScheduledTask -TaskName $Name -Confirm:$false }
} elseif ($Action -in @('install','start','restart')) {
    $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $TaskAction = New-ScheduledTaskAction -Execute $Pythonw -Argument $Arguments -WorkingDirectory $Root
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $Triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $User), (Get-QqRecoveryTrigger))
    Register-ScheduledTask -TaskName $Name -Action $TaskAction -Trigger $Triggers -Principal $Principal -Settings (Get-QqTaskSettings) -Force | Out-Null
    Enable-ScheduledTask -TaskName $Name | Out-Null
    if ((Get-ScheduledTask -TaskName $Name).State -ne 'Running') { Start-ScheduledTask -TaskName $Name }
}
