# Review geral — US02 Captura e versionamento do GTFS

Data: 18/11/2026 · Branch: `review/US02-geral` (a partir de `main` em `d0a2176`)
Escopo: US02-T01, T02A, T02B, T03 e T04. Referências: documento de requisitos
e diagramas de classes, componentes e casos de uso de 18/11/2026.

Critério oficial: *verificação diária; versão nova na base em até 24 h; cada
versão com data de captura e hash; cada observação ligada à versão vigente no dia.*

Legenda: **Implementado** = existe código · **Testado** = coberto por teste
automatizado · **Comprovado** = executado contra a fonte real e MySQL 8.

## Critérios

| # | Critério | Situação | Evidência | Pendência |
|---|----------|----------|-----------|-----------|
| 1 | Verificação diária | Implementado e testado | `app/gtfs/scheduler.py`, `python -m app.tarefas`; `tests/gtfs/test_scheduler.py` (cadência de 24 h, retomada, nova tentativa em 15 min, lock) | Processo contínuo nunca executado em operação real; não há serviço/cron instalado (decisão de implantação) |
| 2 | Nova versão na base em até 24 h | Implementado e testado para `VersaoGTFS` | `VerificacaoGTFS.identificada_em/persistida_em`, estado `PARCIAL` + log `gtfs_prazo_excedido` | A rede (linhas/paradas/trechos) **não** é importada pelo agendador, só por `python -m app.gtfs` (ver P2) |
| 3 | Hash e data de captura | Implementado, testado, comprovado | SHA-256 único (`uq_versoes_gtfs_hash`), `capturada_em` com fuso; `tests/test_gtfs.py`; download real + MySQL 8 local em `docs/review-US02-T01.md` | — |
| 4 | Linhas, paradas e trechos por versão | Implementado e testado | Chaves `(versao_gtfs_id, codigo)` e FKs compostas em `trechos`; `tests/test_gtfs_rede.py` | Migrações `0006`/`0007` não executadas em MySQL 8 depois da renumeração (P4) |
| 5 | Versão vigente por data operacional | Implementado e testado | `versao_vigente(date)` em `app/gtfs/service.py`; `tests/gtfs/test_vigencia.py` | **Na prática gera ambiguidade** a partir da 2ª versão sem `feed_info.txt` (P1) |
| 6 | Ausência/ambiguidade explícitas | Implementado e testado | `VersaoGTFSInexistente`, `VersaoGTFSAmbigua`; recusa `datetime` | — |
| 7 | Histórico preservado e vínculo de observações | Parcial | ZIP imutável no FileStore, versões e rede nunca sobrescritas, FKs `RESTRICT`; `ObservacaoTratada.versao_gtfs_id` (US12) | Gravar o vínculo de cada observação é da **US03** (MatchingService), não concluído aqui |

## Revisão da integração

- **Cadeia Alembic quebrada na `main`.** `0006_linhas_paradas_gtfs.py` e
  `0007_trechos_gtfs.py` declaravam `revision` `0004`/`0005`, duplicando as
  revisões da US12 e da T04 (três cabeças: `0004`, `0005`, `0005`). O teste
  `test_cadeia_de_migracoes_tem_uma_cabeca_e_preserva_a_us12` falhava na `main`.
  A correção já existia fora da `main` (`bb0f053` em `us02-t02a-e--us02-t02b`
  e `c1fa48c` em `fix/cadeia-migracoes-alembic`). Foi aplicada a `c1fa48c`,
  que também corrige as mensagens de downgrade. Cadeia resultante:
  `0001 → … → 0005 → 0006 → 0007`, cabeça única `0007`. O SQL offline
  (`flask db upgrade --sql 0005:0007`, URL fictícia, sem conexão) confere com
  os modelos.
- **Modelos × migrações:** `linhas`, `paradas`, `trechos`, `verificacoes_gtfs`
  e `versoes_gtfs` coincidem em colunas, chaves, collation binária e checks.
- **Compatibilidade entre tarefas:** T02A/T02B dependem do ZIP e do hash da
  T01 (`_abrir_versao` confere o hash); T04 reutiliza
  `capturar_gtfs_com_resultado` sem duplicar a captura; T03 lê apenas a
  vigência gravada pela T01. Não há duplicação de serviços.
- **Divergências do diagrama de classes (intencionais e documentadas):**
  `Linha`/`Parada` usam chave `(versão, código)`, sem a classe ficar global;
  `Trecho` ganhou `trajeto` (`trip_id` representativo) e as paradas por código;
  `VerificacaoGTFS` não está no diagrama (o mais próximo é a proposta
  `ExecucaoProcessamento`). Recomenda-se atualizar o diagrama.

## Correções feitas nesta review

1. Renumeração de `0006`/`0007` (cherry-pick de `c1fa48c`) e ajuste do teste
   da cadeia.
2. `importar_linhas_paradas` não fechava o ZIP ao retornar cedo numa versão já
   importada; a checagem foi movida para dentro do `with`, como em
   `importar_trechos`.

## Pendências (decisão do PO ou fora do escopo da review)

- **P1 — Vigência sobreposta (alta).** Sem `feed_info.txt`, a T01 grava
  `vigencia_inicio = data UTC da captura` e `vigencia_fim = NULL`. Reproduzido:
  depois da 2ª captura, qualquer data igual ou posterior a ela gera
  `VersaoGTFSAmbigua`. É preciso decidir a regra: encerrar a versão anterior
  (`vigencia_fim = início da nova − 1 dia`) ao capturar, ou deixar a mais
  recente prevalecer. Confirmar também se o feed da SPTrans traz `feed_info.txt`.
- **P2 — Importação da rede fora do agendador (média).** `verificar_gtfs` só
  captura o ZIP. Linhas, paradas e trechos de uma versão nova só entram na base
  com `python -m app.gtfs`. É preciso decidir se a importação passa a fazer
  parte do job diário, o que afeta o prazo de 24 h.
- **P3 — Fuso da data de captura (baixa).** O fallback de `vigencia_inicio` usa
  a data UTC; uma captura depois das 21:00 em Brasília cai no dia seguinte.
- **P4 — Validação em MySQL 8 (média).** Rodar `flask db upgrade` de `0001` a
  `0007` num MySQL 8 isolado, já que só havia MariaDB local e o Docker não
  estava acessível. Antes de aplicar na Aiven, conferir `alembic_version`
  (com IDs duplicados, o Alembic recusaria aplicar `0006`/`0007`).
- **P5 — US03.** Vincular cada observação à versão vigente com `versao_vigente`.
- **P6 — Branches órfãs.** `us02-t02a-e--us02-t02b` (`bb0f053`) e
  `fix/cadeia-migracoes-alembic` (`c1fa48c`) ficam redundantes depois do merge
  desta review.

## Testes

- Linha de base na `main` (`d0a2176`): 131 passaram, **1 falhou** (cadeia de
  migrações), 3 ignorados.
- Depois da review: `pytest` completo, **132 passaram, 3 ignorados**
  (dois ensaios MySQL opt-in sem URL e a consulta real opcional à SPTrans).
- Testes específicos de GTFS (`tests/test_gtfs.py`, `tests/test_gtfs_rede.py`,
  `tests/gtfs/`): **64 passaram**.
- Nenhuma migração, consulta ou escrita foi feita na Aiven.

## Parecer

**US02 parcialmente aceita.** Captura, hash, histórico imutável, rede por
versão, agenda diária e tratamento explícito de ausência/ambiguidade estão
implementados e testados. O aceite completo depende de P1 (que hoje torna a
vigência inutilizável depois da 2ª versão sem `feed_info.txt`), P2 e da
validação P4 em MySQL 8. O vínculo das observações pertence à US03.
