#Requires -Version 5.1
<# Run Install/Remove/Start/Stop in an elevated PowerShell opened by the SAME developer user.
   Only frpc runs as LocalService. The IDE task always uses the developer account.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Remove','Start','Stop','Status')][string]$Action,
    [string]$Root = "$env:USERPROFILE\RemoteCode",
    [string]$NssmPath,
    [string]$NssmSha256,
    [switch]$Headless
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $Root).Path
$Config = Get-Content -LiteralPath (Join-Path $Root 'local.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Config.owner_sid -and $Config.owner_sid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { throw 'Use the same account that installed this runtime.' }
if ($Config.id -notmatch '^[a-z][a-z0-9-]{0,24}$') { throw 'Invalid machine ID' }
$Name = "RemoteCode-frpc-$($Config.id)"
$TaskName = "RemoteCode-IDE-$($Config.id)"
$ServiceRoot = Join-Path $env:ProgramData "RemoteCode\$($Config.id)"
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Action -ne 'Status' -and -not $IsAdmin) { throw 'Run elevated PowerShell as the same developer account.' }

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Native command failed ($LASTEXITCODE): $Exe" }
}

function Assert-OwnedObjects {
    $Service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Service -or $Task -or (Test-Path -LiteralPath $ServiceRoot)) {
        $OwnerPath = Join-Path $ServiceRoot 'owner.json'
        if (-not (Test-Path -LiteralPath $OwnerPath)) { throw 'Existing service/task directory is unmanaged. Refusing to modify it.' }
        $Owner = Get-Content -LiteralPath $OwnerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not [string]::Equals($Owner.root, $Root, [StringComparison]::OrdinalIgnoreCase) -or $Owner.machine -ne $Config.id) { throw 'Service directory belongs to another runtime.' }
    }
    if ($Task) {
        $Expected = Get-Content -LiteralPath (Join-Path $Root 'windows-task.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $Actions = @($Task.Actions)
        if ($Actions.Count -ne 1 -or -not [string]::Equals($Actions[0].Execute, $Expected.executable, [StringComparison]::OrdinalIgnoreCase) -or $Actions[0].Arguments -ne $Expected.arguments -or $Actions[0].WorkingDirectory -ne $Expected.working_directory) { throw 'IDE task belongs to a different runtime or has been modified.' }
        $TaskUser = $Task.Principal.UserId
        if ($TaskUser -notmatch '^S-1-') { $TaskUser = (New-Object Security.Principal.NTAccount($TaskUser)).Translate([Security.Principal.SecurityIdentifier]).Value }
        if ($TaskUser -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { throw 'IDE task belongs to another developer account.' }
    }
    if ($Service) {
        $ServiceInfo = Get-CimInstance Win32_Service -Filter "Name='$Name'"
        if (-not [string]::Equals($ServiceInfo.PathName.Trim('"'), (Join-Path $ServiceRoot 'nssm.exe'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Tunnel service binary is not owned by this deployment.' }
    }
}
if ($Action -ne 'Status') { Assert-OwnedObjects }

switch ($Action) {
    'Install' {
        $ExistingService = Get-Service -Name $Name -ErrorAction SilentlyContinue
        $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($ExistingService -and $ExistingTask) {
            $ServiceCA = (Join-Path $ServiceRoot 'ca.pem').Replace('\','/')
            $ExpectedFrpc = Get-Content -LiteralPath (Join-Path $Root 'frpc.toml') -Raw -Encoding UTF8
            $ExpectedFrpc = [regex]::Replace($ExpectedFrpc, '(?m)^transport\.tls\.trustedCaFile = .*$', ('transport.tls.trustedCaFile = "' + $ServiceCA + '"'))
            if ((Get-Content -LiteralPath (Join-Path $ServiceRoot 'frpc.toml') -Raw -Encoding UTF8) -cne $ExpectedFrpc -or (Get-FileHash -LiteralPath (Join-Path $ServiceRoot 'ca.pem')).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Root 'ca.pem')).Hash -or (Get-FileHash -LiteralPath (Join-Path $ServiceRoot 'frpc.exe')).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Root 'bin\frpc.exe')).Hash) { throw 'Service copy differs from runtime. Remove services and reinstall after pairing/upgrading.' }
            if ($Headless -and -not ($ExistingTask.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' })) { throw 'Existing task starts at logon. Remove it first to switch to -Headless.' }
            if ($ExistingTask.State -ne 'Running') { Start-ScheduledTask -TaskName $TaskName }
            if ($ExistingService.Status -ne 'Running') { Start-Service -Name $Name }
            Write-Host 'Existing services verified; retained task credentials and running sessions.'
            break
        }
        # Recover only our own partial registration left by an interrupted install.
        if ($ExistingTask) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        if ($ExistingService) {
            Stop-Service -Name $Name -ErrorAction SilentlyContinue
            Invoke-Native (Join-Path $ServiceRoot 'nssm.exe') @('remove', $Name, 'confirm')
        }
        if (Test-Path -LiteralPath $ServiceRoot) { Remove-Item -LiteralPath $ServiceRoot -Recurse -Force }
        if (-not $NssmPath -or $NssmSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Provide a trusted NSSM executable and its verified SHA-256.' }
        $NssmPath = (Resolve-Path -LiteralPath $NssmPath).Path
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $NssmPath).Hash -ne $NssmSha256) { throw 'NSSM SHA-256 mismatch' }
        if ((Test-Path -LiteralPath $ServiceRoot) -and (Get-ChildItem -LiteralPath $ServiceRoot -Force | Select-Object -First 1)) { throw 'Service directory is not empty. Refusing to overwrite it.' }
        New-Item -ItemType Directory -Path $ServiceRoot -Force | Out-Null
        Invoke-Native 'icacls.exe' @($ServiceRoot, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-19:(OI)(CI)RX', '/Q')
        @{ root = $Root; machine = $Config.id } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ServiceRoot 'owner.json') -Encoding UTF8
        Copy-Item -LiteralPath $NssmPath -Destination (Join-Path $ServiceRoot 'nssm.exe')
        Copy-Item -LiteralPath (Join-Path $Root 'bin\frpc.exe') -Destination $ServiceRoot
        Copy-Item -LiteralPath (Join-Path $Root 'ca.pem') -Destination $ServiceRoot
        $ServiceCA = (Join-Path $ServiceRoot 'ca.pem').Replace('\','/')
        $FrpcText = Get-Content -LiteralPath (Join-Path $Root 'frpc.toml') -Raw -Encoding UTF8
        $FrpcText = [regex]::Replace($FrpcText, '(?m)^transport\.tls\.trustedCaFile = .*$', ('transport.tls.trustedCaFile = "' + $ServiceCA + '"'))
        [IO.File]::WriteAllText((Join-Path $ServiceRoot 'frpc.toml'), $FrpcText, (New-Object Text.UTF8Encoding($false)))
        New-Item -ItemType Directory -Path (Join-Path $ServiceRoot 'logs') -Force | Out-Null
        # Existing files need effective ACEs; inheritance-only ACEs leave regular
        # files unreadable after a recursive icacls pass.
        Invoke-Native 'icacls.exe' @($ServiceRoot, '/inheritance:r', '/grant:r', '*S-1-5-18:F', '*S-1-5-32-544:F', '*S-1-5-19:RX', '/T', '/Q')
        Invoke-Native 'icacls.exe' @($ServiceRoot, '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-19:(OI)(CI)RX', '/Q')
        Invoke-Native 'icacls.exe' @((Join-Path $ServiceRoot 'logs'), '/grant:r', '*S-1-5-19:(OI)(CI)M', '/Q')
        $Nssm = Join-Path $ServiceRoot 'nssm.exe'
        Invoke-Native (Join-Path $ServiceRoot 'frpc.exe') @('verify', '-c', (Join-Path $ServiceRoot 'frpc.toml'))
        Invoke-Native $Nssm @('install', $Name, (Join-Path $ServiceRoot 'frpc.exe'))
        try {
            Invoke-Native $Nssm @('set', $Name, 'AppDirectory', $ServiceRoot)
            Invoke-Native $Nssm @('set', $Name, 'AppParameters', ('-c "' + (Join-Path $ServiceRoot 'frpc.toml') + '"'))
            Invoke-Native $Nssm @('set', $Name, 'ObjectName', 'NT AUTHORITY\LocalService')
            Invoke-Native $Nssm @('set', $Name, 'Start', 'SERVICE_DELAYED_AUTO_START')
            Invoke-Native $Nssm @('set', $Name, 'AppExit', 'Default', 'Restart')
            Invoke-Native $Nssm @('set', $Name, 'AppRestartDelay', '5000')
            Invoke-Native $Nssm @('set', $Name, 'AppStdout', (Join-Path $ServiceRoot 'logs\stdout.log'))
            Invoke-Native $Nssm @('set', $Name, 'AppStderr', (Join-Path $ServiceRoot 'logs\stderr.log'))
            Invoke-Native $Nssm @('set', $Name, 'AppRotateFiles', '1')
            Invoke-Native $Nssm @('set', $Name, 'AppRotateOnline', '1')
            Invoke-Native $Nssm @('set', $Name, 'AppRotateBytes', '5242880')
            # Python uses Windows argv quoting, including paths ending in a backslash.
            $TaskConfig = Get-Content -LiteralPath (Join-Path $Root 'windows-task.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            $Node = $TaskConfig.executable
            $TaskArgs = $TaskConfig.arguments
            $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $TaskAction = New-ScheduledTaskAction -Execute $Node -Argument $TaskArgs -WorkingDirectory $TaskConfig.working_directory
            $TaskSettings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            if ($Headless) {
                # Task Scheduler stores the password; the script never writes it to disk.
                $Credential = Get-Credential -UserName $User -Message 'Developer account password for boot-time IDE task (not PIN)'
                if ($Credential.UserName -ne $User) { throw 'Use the same developer account.' }
                $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Credential.Password)
                try {
                    $Plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr)
                    Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $TaskSettings -User $User -Password $Plain -RunLevel Limited | Out-Null
                } finally {
                    $Plain = $null
                    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Ptr)
                }
            } else {
                $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
                Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $User) -Settings $TaskSettings -Principal $Principal | Out-Null
            }
            Start-ScheduledTask -TaskName $TaskName
            Start-Service -Name $Name
        } catch {
            Stop-Service -Name $Name -ErrorAction SilentlyContinue
            Invoke-Native $Nssm @('remove', $Name, 'confirm')
            if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
                Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            }
            throw
        }
        Write-Host 'Installed. The default IDE task requires user login; -Headless uses a boot task.'
    }
    'Stop' {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-Service -Name $Name -ErrorAction SilentlyContinue
    }
    'Start' {
        Start-ScheduledTask -TaskName $TaskName
        Start-Service -Name $Name
    }
    'Remove' {
        if (Test-Path -LiteralPath $ServiceRoot) {
            $OwnerPath = Join-Path $ServiceRoot 'owner.json'
            if (-not (Test-Path -LiteralPath $OwnerPath)) { throw 'Refusing to remove an unmanaged service directory.' }
            $Owner = Get-Content -LiteralPath $OwnerPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if (-not [string]::Equals($Owner.root, $Root, [StringComparison]::OrdinalIgnoreCase) -or $Owner.machine -ne $Config.id) { throw 'Service directory belongs to another runtime.' }
        }
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
            Stop-Service -Name $Name -ErrorAction SilentlyContinue
            Invoke-Native (Join-Path $ServiceRoot 'nssm.exe') @('remove', $Name, 'confirm')
        }
        if (Test-Path -LiteralPath $ServiceRoot) { Remove-Item -LiteralPath $ServiceRoot -Recurse -Force }
        Write-Host 'Runtime services removed. Use local.py uninstall --purge to remove the home runtime.'
    }
    'Status' {
        Get-Service -Name $Name -ErrorAction SilentlyContinue
        Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Get-ScheduledTaskInfo
        Get-NetTCPConnection -LocalPort $Config.local_port -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess
    }
}
