# Review integrada — US28 Extrações assíncronas de brutos

Data: 27/11/2026 · Branch: `review/us28-extracoes-assincronas` (backend e frontend) · Base: `main`
com US28-T01, T02 e T03A (PRs #68–#70) e T03B (frontend PR #24) já integradas.

Fluxo revisado: `POST /extracoes` → `PedidoExtracao` + `ExecucaoProcessamento` (mesma transação,
`JobService`) → `python -m app.jobs --tipo EXTRACAO_BRUTOS` → `escrever_brutos` (núcleo da US09) →
`FileStore` + `ArquivoArmazenado` → `GET /extracoes/{id}` → `brutos.html` → download.
A fila é a própria tabela de jobs no MySQL: não há fila externa a falhar.

## Problemas e correções

| # | Tarefa | Prioridade | Problema | Correção |
|---|---|---|---|---|
| 1 | T02 | P1 | Sem índice por linha: a paginação por `(coletado_em, id)` varre os brutos de **todas** as linhas do mês (≈1 bilhão num mês real) para achar os de uma. Projeção de ~40 min só de varredura. | Migração `us28_0029`: índice `(linha_origem, coletado_em, id)`; declarado no modelo. Plano passa a `range` coberto (`Using index`). |
| 2 | T02 | P1 | A limpeza de temporários lia a posse dos jobs no snapshot REPEATABLE READ do worker. Um job reservado por outro worker depois desse snapshot parecia sem dono e o seu temporário **vivo** era apagado (reproduzido no MySQL 8). | Lista os arquivos e depois lê a posse numa conexão nova: todo arquivo listado já tem a reserva visível. |
| 3 | T03A | P2 | `progresso` sempre presente (`registros: 0` em `aguardando`) e, após uma tentativa falhar, a contagem antiga aparecia com o pedido de volta a `aguardando`/`erro`. | `progresso` só em `processando`/`pronto` com registros gravados pela tentativa atual; senão `null`. |
| 4 | T03A | P2 | Nome do download vinha do `ArquivoArmazenado`, que é endereçado pelo conteúdo e compartilhado (ex.: CSVs vazios de linhas diferentes): o pedido da linha B baixava "brutos_linha_A…". | `ExportService.nome_arquivo(pedido)` usado na API, no download e no registro. |
| 5 | T03B | P2 | Uma falha temporária (rede, 503 com `Retry-After`) encerrava o polling de um pedido não final. | Nova consulta automática com espera dobrada (5 s → 60 s), aviso visível, "Tentar novamente" mantido. |
| 6 | T03B | P2 | POST lento que terminava depois de o usuário passar a acompanhar outro pedido assumia o acompanhamento. | Guarda de troca de pedido; o novo código é informado na mensagem. |

Sem defeitos encontrados: validação e 400, id opaco, 202 sem trabalho pesado, rollback conjunto,
idempotência (8 POSTs simultâneos → 1 pedido), reserva atômica, `FOR UPDATE` com token na publicação,
PRONTO só com arquivo publicado, checagem do arquivo na consulta e hash completo no download,
retomada idempotente por conteúdo, metadados de fonte/licença CC BY 4.0, CORS.

## Testes

| Conjunto | Resultado |
|---|---|
| Backend completo (SQLite) | 1400 passed, 25 skipped (opt-in MySQL) |
| US28 + US20 no **MySQL 8.4.7 isolado** | 15 passed (6 US28, dos quais 4 novos; 9 US20) |
| Frontend completo (`node --test`) + `test_servidor.py` | 206 passed + OK |
| Navegador real (Chrome headless) contra Flask + worker + MySQL 8 isolado | aguardando, processando, recarga, pronto, erro, 404, queda e volta da API |

Novos testes MySQL (`tests/test_pedidos_extracao_mysql.py`, variável `US28_REVIEW_MYSQL_TEST_URL`,
base `us28_verificacao_*`; cada teste cria a sua base no servidor isolado):
falha real de FK entre job e pedido sem órfãos; worker em outro processo morto no meio da gravação →
reinício do Flask → retomada sem duplicar → download íntegro; falha de publicação nunca vira `pronto`;
arquivo final ausente → `erro`; brutos com o mesmo checksum antes e depois; limpeza concorrente;
3 workers simultâneos em 4 pedidos (cada job uma tentativa).

## Desempenho (requisito: um mês de uma linha em até 15 min)

Ensaio local (MySQL 8.4.7, 2,23 milhões de brutos no mês, 446.400 da linha pedida — um mês completo
de 5 veículos a cada 30 s): **15,7 s (Parquet) e 17,1 s (CSV, 130 MB)** pelo worker real; pico de
memória 250–325 MB, quase constante entre 1 dia e 1 mês. Detalhes em
`docs/evidencias/US28-review-2026-11-27.json`. **Não comprovado** na Aiven nem com o histórico real
completo: a projeção sem índice (~40 min) motiva a migração.

## Pendências e decisões do PO

- Aplicar `us28_0029` na Aiven (não executado nesta review). No MySQL 8 o `CREATE INDEX` é online;
  acrescenta um índice à tabela de maior escrita (~375 inserções/s com a frota completa).
- Medir o tempo na Aiven com volume real após a migração.
- "Aviso quando estiver pronto": atendido na tela (estado `pronto` + download). Não há notificação
  fora da página (e-mail/push), e nada na interface promete isso.
- Valores a confirmar: período máximo de 31 dias, 3 tentativas por job, reserva de 15 min.
- `erro` expõe o tipo da exceção (ex.: `PermissionError`), sem mensagem nem caminho.
