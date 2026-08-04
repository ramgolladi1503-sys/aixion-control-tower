param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,

    [Parameter(Mandatory = $true)]
    [string]$Config,

    [string]$TaskName = "AixionAgentRelay"
)

$ErrorActionPreference = "Stop"

$ExecutablePath = (Resolve-Path -LiteralPath $Executable).Path
$ConfigPath = (Resolve-Path -LiteralPath $Config).Path

if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "Relay executable does not exist: $ExecutablePath"
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Relay configuration does not exist: $ConfigPath"
}

$Arguments = "--config `"$ConfigPath`" run"
$Action = New-ScheduledTaskAction -Execute $ExecutablePath -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 20 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Outbound-only Aixion universal agent relay. Credentials remain in Windows Credential Manager or an external secret manager."

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Inspect with: Get-ScheduledTask -TaskName $TaskName"
Write-Host "The relay token is not stored in the scheduled-task definition."
