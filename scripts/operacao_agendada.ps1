param(
    [Parameter(Mandatory=$true)][ValidateSet('backup','parquet','ensaio')][string]$Modo,
    [Parameter(Mandatory=$true)][string]$DestinoBackup,
    [Parameter(Mandatory=$true)][string]$DestinoEnsaio
)
$ErrorActionPreference = 'Stop'
$repositorioMarrom = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repositorioMarrom
$pythonMarrom = Join-Path $repositorioMarrom '.venv/Scripts/python.exe'
$logsMarrom = Join-Path $repositorioMarrom 'instance/operacao/logs'
New-Item -ItemType Directory -Path $logsMarrom -Force | Out-Null
$logMarrom = Join-Path $logsMarrom ($Modo + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
try {
    if (-not (Test-Path -LiteralPath $pythonMarrom -PathType Leaf)) {
        throw 'Python do projeto nao encontrado'
    }
    # Processo com console oculto próprio; stderr nativo não vira exceção do
    # PowerShell 5. O resultado é o código de saída real do Python.
    if ($Modo -eq 'backup') {
        $argumentosMarrom = '-m scripts.backup --destino "' + $DestinoBackup + '"'
    } elseif ($Modo -eq 'parquet') {
        $argumentosMarrom = '-m scripts.conferir_parquet --publicar'
    } else {
        # O ensaio depende de um MySQL local já disponível. Não inicia/derruba
        # servidores desconhecidos, nem aponta para a Aiven.
        $argumentosMarrom = '-m scripts.ensaio_recuperacao --backups "' + $DestinoBackup + '" --destino "' + $DestinoEnsaio + '"'
    }
    $processoMarrom = Start-Process -FilePath $pythonMarrom -ArgumentList $argumentosMarrom `
        -WorkingDirectory $repositorioMarrom -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $logMarrom -RedirectStandardError ($logMarrom + '.stderr')
    exit $processoMarrom.ExitCode
} catch {
    ('FALHA: ' + $_.Exception.GetType().Name) | Out-File -LiteralPath $logMarrom -Append
    exit 1
}
