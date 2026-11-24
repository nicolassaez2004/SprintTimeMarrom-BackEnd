# Coletor contínuo (US01/US29) — iniciar, monitorar e parar

O coletor é um processo separado do Flask. Ele consulta `GET /Posicao` da
SPTrans a cada `GPS_COLLECTION_INTERVAL_SECONDS` (padrão 30 s) e grava em
`DATABASE_URL`. Abrir o site ou reiniciar a API não cria outro coletor.

> **Aiven:** coleta contínua na base compartilhada só com autorização da
> equipe. Para ensaios, usar uma base local (`flask --app app db upgrade`).

## Iniciar

Na raiz do backend, com o `.venv` e o `.env` (`DATABASE_URL`, `SPTRANS_TOKEN`):

```bash
python -m app.coleta.bus_collector              # contínuo, até Ctrl+C
python -m app.coleta.bus_collector --ciclos 2   # amostra limitada
```

Configure `COLETA_STATE_DIR` num diretório persistente. Ele guarda o
checkpoint (`estado.json`, gravado de forma atômica) e a caixa local de
alertas. Configure também `ALERTA_WEBHOOK_URL` para avisar a equipe após
10 min de falha (US29).

### Linux (systemd do usuário)

`~/.config/systemd/user/marrom-coletor.service`:

```ini
[Unit]
Description=SprintTimeMarrom - coletor GPS
After=network-online.target

[Service]
WorkingDirectory=/caminho/SprintTimeMarrom-BackEnd
ExecStart=/caminho/SprintTimeMarrom-BackEnd/.venv/bin/python -m app.coleta.bus_collector
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

`systemctl --user enable --now marrom-coletor` inicia o serviço. Para ver os
logs, use `journalctl --user -u marrom-coletor -f`.

### Windows (Agendador de Tarefas)

Em PowerShell, na raiz do backend. **Não foi testado nesta revisão, porque não
havia Windows disponível.** O padrão é o mesmo de `scripts/agendar_operacao.ps1`.

```powershell
$raiz = (Get-Location).Path
$acao = New-ScheduledTaskAction -Execute 'cmd.exe' -WorkingDirectory $raiz `
  -Argument '/c .venv\Scripts\python.exe -m app.coleta.bus_collector >> instance\coleta-worker.log 2>&1'
$config = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'SprintMarrom-coleta' -Action $acao -Settings $config `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
```

O computador precisa ficar ligado, com a sessão aberta. Uma máquina desligada
gera lacunas.

## Monitorar

| Onde | O que mostra |
|---|---|
| Log do processo | Uma linha por ciclo: `Ciclo <id>: OK, tentativas=1, novos=N` (ou `FALHA`) |
| `python -m app.monitoramento metricas --inicio <ISO> --fim <ISO>` | Ciclos esperados e gravados, e o percentual gravado em até 60 s (US29) |
| `python -m app.monitoramento alertas` | Falhas de 10 min e recuperações |
| `GET /api/qualidade?inicio=AAAA-MM-DD` e aba Qualidade | Lacunas com início, fim e causa (US13) |
| Mapa ao vivo | "Última coleta às HH:MM:SS"; com mais de 60 s, mostra **coleta atrasada** |
| `python -m scripts.medir_defasagem_mapa --api http://HOST:5000 --duracao 300` | Defasagem entre coleta e tela, usando só GETs HTTP na API |

## Parar

Use Ctrl+C, `systemctl --user stop marrom-coletor` ou
`Stop-ScheduledTask SprintMarrom-coleta`. Uma parada abrupta (SIGTERM ou
queda) também é segura: o banco libera o `GET_LOCK`, e o ciclo incompleto
vira `FALHA` na próxima partida. O intervalo parado é registrado como lacuna
`PROCESSO_INTERROMPIDO`, que **nunca é preenchida** com posições.

## Execução duplicada

Uma segunda instância é recusada em duas camadas:

- **No mesmo computador:** o lock de arquivo do checkpoint recusa com "Outro
  processo utiliza o checkpoint da coleta".
- **Em outra máquina ou diretório:** o `GET_LOCK` do MySQL recusa com "Outro
  coletor possui o lock MySQL".

Ambas as recusas foram reproduzidas em 24/11/2026. Use **uma** máquina coletora por base.

## Desempenho da gravação (24/11/2026)

A Aiven tem RTT medido de 218 ms. A deduplicação agora consulta pares exatos
no índice único `(prefixo_origem, horario_gps)`. Antes, ela lia todo o
histórico dos prefixos (342 mil linhas por ciclo na Aiven, número que cresce
com os dias). Os `INSERT`s também passaram a usar lotes de 2000 linhas. Com um
proxy local de 218 ms, cada ciclo passou de 14–20 s para 8,7–9,9 s. A
evidência está em `docs/reviews/sprint1-mvp.md`.
