# US02-T01 — Correções do review

Branch: `REVIEW-US02-T01]-Capturar-o-GTFS-original-com-data-e-hash`.
Base atualizada por fast-forward para `origin/main` no commit `2e08cd6`.

## Correções

- `app/storage.py` concentra publicação e integridade dos ZIPs no FileStore.
- A captura publica/verifica o original antes de reutilizar uma versão por hash. Uma cópia ausente é recuperada do download completo já validado, sem duplicar versão/metadados nem alterar a data original.
- Conteúdo divergente no destino causa erro e é preservado para diagnóstico; não é sobrescrito silenciosamente.
- Rollback não remove objetos finais endereçados por hash. Isso impede que a limpeza de uma captura apague o objeto que outra captura vai confirmar. Só temporários privados são removidos imediatamente.
- `GTFS_STORAGE_DIR` permite configurar o diretório do FileStore. O README explicita que banco Aiven compartilhado não compartilha disco: consumidores precisam do volume acessível ou de um único coletor/armazenamento. A captura em um segundo diretório também garante seu ZIP.

## Evidência em 18/11/2026

Suíte completa após as correções: **69 testes aprovados, 1 ignorado e 14 subtestes aprovados**. `git diff --check` passou. Última conferência de `origin/main`: `2e08cd69462a4330f4639e60a0b04e0e3ee0735c`, igual ao HEAD da branch de review antes do commit das correções.

Testes GTFS cobrem: bytes/hash original, conteúdo diferente, falha de download/ZIP inválido, recuperação de arquivo ausente/data UTC, dois diretórios com mesmos metadados, corrupção sem sobrescrita, diretório configurado, falha SQL seguida de retomada, duas capturas concorrentes com falha intermediária e duas capturas concorrentes bem-sucedidas.

Também foi criado um banco MySQL **local e descartável**, separado das bases da Aiven, para executar:

```text
flask --app app db upgrade: OK (0001 → 0002 → 0003)
flask --app app db check: OK (sem diferenças pendentes)
python -m app.gtfs: OK (download real da SPTrans)
python -m app.gtfs: OK (repetição sem duplicação)
versoes_gtfs: 1 linha
arquivos_armazenados: 1 linha
SHA-256 recalculado do ZIP: 3141809bcbb8c1677244bd3e4499faa612857d9685d056317b137dc1d0195655
```

A base descartável foi removida ao final. Nenhuma migração, coleta ou alteração nas bases Aiven foi executada durante esta correção. Os ensaios concorrentes usam SQLite e arquivos temporários; as migrações e o fluxo real foram verificados em MySQL 8.0.46 local.

## Comportamento após falha

Podem existir ZIPs íntegros sem metadados após rollback SQL; a próxima tentativa os reutiliza. Essa é uma retenção deliberada para evitar perda concorrente. Limpeza posterior de órfãos precisa de coordenação e não foi automatizada nesta tarefa. Arquivos de versões antigas não são automaticamente replicados entre computadores pelo MySQL.

## Conclusão

As duas falhas bloqueantes do review foram corrigidas e cobertas por testes. Alterações ficam na branch de review, sem commit/push feitos pelo agente. Aprovar o diff, comitar e integrar à main. A ausência de conflitos é válida em relação à main conferida e deve ser reconferida se outros commits entrarem antes do merge.
