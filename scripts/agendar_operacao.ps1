param(
    [Parameter(Mandatory=$true)][string]$DestinoBackup,
    [Parameter(Mandatory=$true)][string]$DestinoEnsaio
)
$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'E. South America Standard Time') {
    throw 'Configure o Windows no fuso de Sao Paulo antes de agendar.'
}
$repositorioMarrom = Split-Path -Parent $PSScriptRoot
$runnerMarrom = Join-Path $PSScriptRoot 'operacao_agendada.ps1'
$usuarioMarrom = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principalMarrom = New-ScheduledTaskPrincipal -UserId $usuarioMarrom -LogonType Interactive -RunLevel Limited
$settingsMarrom = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 24) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($modoMarrom in @('backup','parquet','ensaio')) {
    $argsMarrom = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runnerMarrom +
        '" -Modo ' + $modoMarrom + ' -DestinoBackup "' + $DestinoBackup + '" -DestinoEnsaio "' + $DestinoEnsaio + '"'
    $acaoMarrom = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argsMarrom -WorkingDirectory $repositorioMarrom
    if ($modoMarrom -eq 'backup') {
        $gatilhoMarrom = New-ScheduledTaskTrigger -Daily -At '02:00'
    } elseif ($modoMarrom -eq 'parquet') {
        $gatilhoMarrom = New-ScheduledTaskTrigger -Daily -At '00:15'
        # Tentativas de 15 em 15 min até 05:45, antes do prazo das 06:00.
        $gatilhoMarrom.Repetition = (New-ScheduledTaskTrigger -Once -At '00:15' `
            -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 45)).Repetition
    } else {
        # O agendador mensal com intervalo de seis meses registra o processo,
        # não afirma que o ensaio futuro já foi executado.
        $xmlAcaoMarrom = [System.Security.SecurityElement]::Escape($argsMarrom)
        $xmlPastaMarrom = [System.Security.SecurityElement]::Escape($repositorioMarrom)
        $xmlUsuarioMarrom = [System.Security.SecurityElement]::Escape($usuarioMarrom)
        $xmlMarrom = @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><CalendarTrigger><StartBoundary>2027-05-20T10:00:00</StartBoundary><Enabled>true</Enabled><ScheduleByMonth><DaysOfMonth><Day>20</Day></DaysOfMonth><Months><May/><November/></Months></ScheduleByMonth></CalendarTrigger></Triggers>
<Principals><Principal id="Author"><UserId>$xmlUsuarioMarrom</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT24H</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
<Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>$xmlAcaoMarrom</Arguments><WorkingDirectory>$xmlPastaMarrom</WorkingDirectory></Exec></Actions>
</Task>
"@
        Register-ScheduledTask -TaskName 'SprintMarrom-ensaio-semestral' -Xml $xmlMarrom -Force | Out-Null
        continue
    }
    Register-ScheduledTask -TaskName ('SprintMarrom-' + $modoMarrom + '-diario') -Action $acaoMarrom `
        -Trigger $gatilhoMarrom -Settings $settingsMarrom -Principal $principalMarrom -Force | Out-Null
}
Get-ScheduledTask -TaskName 'SprintMarrom-*' | Select-Object TaskName,State
