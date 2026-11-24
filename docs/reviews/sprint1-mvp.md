# Review da Sprint 1 — MVP SprintTimeMarrom

Data: 24/11/2026. Branch: `review/sprint1-mvp` (backend e frontend), criada a
partir das `main` atualizadas (backend `8753761`, frontend `89fcf8c`).
Sem commits, pushes ou merges. Revisão técnica feita pelo Claude Code.

**Meta da Sprint 1:** coletar, armazenar e integrar dados da SPTrans,
disponibilizando um primeiro mapa funcional dos ônibus.

## Resumo

| Pergunta | Resposta |
|---|---|
| A meta da Sprint foi atingida? | **Sim, tecnicamente.** O fluxo SPTrans → mapa funcionou com dados reais. |
| O MVP pode ser demonstrado? | **PRONTO.** Exige a correção do `servidor.py` desta review e o coletor em execução durante a demonstração. |
| A Sprint 1 pode ser aceita? | **Não integralmente.** Nenhuma das 11 histórias cumpre todos os critérios oficiais. A maior parte das pendências é de operação contínua, de migração na Aiven ou de decisão do PO. |

## Como a revisão foi feita

- **Implementação:** leitura do código dos dois repositórios, comparada com o
  PDF de requisitos e os diagramas.
- **Testes automatizados:** suíte completa do backend e do frontend.
- **Integração MySQL:** MariaDB 13 local e descartável, fora da Aiven. Os
  testes US03, US12 e US29, antes ignorados, foram executados nele.
- **Operação real:** um único ciclo do coletor (`--ciclos 1`), consultando a
  SPTrans de verdade e gravando no MariaDB local. Depois, a API Flask e o
  frontend foram abertos no Chrome headless.
- **Aiven:** somente uma consulta de leitura (`START TRANSACTION READ ONLY`),
  para ver a revisão Alembic e o frescor dos dados. Nada foi gravado e
  nenhuma migração foi executada.

**Implementação**, **teste automatizado** e **comprovação em operação real**
são coisas diferentes e aparecem separadas abaixo.

## A. Avaliação das histórias

Legenda: ✅ aprovada · 🟡 parcialmente aprovada · ❌ não aprovada.

| US | Backend | Frontend | Testes e evidências | Pendências que impedem o aceite | Aceite |
|---|---|---|---|---|---|
| **US01** Coleta a cada 30 s | `BusCollector` + `SPTransClient` consultam a frota inteira (`GET /Posicao`). O intervalo pode ser configurado de 15 a 60 s. Grava prefixo, linha (`cl`), sentido, lat/lon, horário GPS e horário da coleta. Campo ausente vira NULL. `UNIQUE (prefixo_origem, horario_gps)` no banco. | — | Testes unitários passam. **Ciclo real nesta review:** 11.283 veículos e 2.178 linhas, gravados em 2,2 s, com 0 duplicados e 0 coordenadas nulas. | Meta de ≥ 99% dos ciclos no mês sem medição, porque não há coleta contínua. Na Aiven, o último ciclo foi em 23/11 às 14:01 UTC (88 ciclos OK no total). A "coleta de garagem", citada no planejamento, não foi implementada (decisão do PO). | 🟡 |
| **US02** GTFS versionado | Verificação diária, hash, data de captura e prazo de 24 h medido (`VerificacaoGTFS`). | Versão do dia na aba Qualidade. | Testes passam. Uma versão real na Aiven. | **Vigência sobreposta:** sem `feed_info.txt`, a 2ª versão deixa toda data ambígua (P1 da review US02, ainda aberta). A rede não é importada pelo agendador. O vínculo de cada observação à versão vigente depende da US03, que hoje roda só manualmente. | 🟡 |
| **US12** Brutos sem alteração | `registros_gps` imutável e `observacoes_tratadas` separada por FK. O reprocessamento é determinístico (US12-T03). | — | Testes passam, incluindo MySQL. A cadeia de migrações US12 também foi validada nesta review. | O critério oficial pede recalcular um mês e obter os mesmos indicadores publicados. Isso depende de US14+, que ainda não existem (`docs/US12-T04.md`). Além disso, os campos de linha da resposta (`c`, `lt0`, `lt1`, `hr`) não são guardados: só o objeto de cada veículo, mais `cl`/`sl`. | ❌ |
| **US04** Partidas por linha/dia | Partidas geradas do GTFS e `GET /partidas`. Importador da programação oficial com procedência. | — | 30 linhas conferidas com o GTFS, mas em SQLite local. | A fonte oficial está marcada como indisponível (`oficial_disponivel=false`). A Aiven tem 0 partidas. Falta o aceite humano da amostra (`US04-T04-P01..P03`). | 🟡 |
| **US03** Olho Vivo ↔ GTFS | `MatchingService` com tabela versionada, validade (município, 100 m, 5 min) e relatório `/qualidade/correspondencias`. | Linha GTFS aparece no popup do mapa, quando existe. | 99,55% de associação numa amostra real (19/11). Teste MySQL passou nesta review. | O processamento só roda por CLI (`python -m app.correspondencia processar`), sem job diário. A taxa foi medida em uma única captura. A correspondência é derivada, não oficial, e precisa de validação do PO. | 🟡 |
| **US29** Coleta resiliente | Até 5 novas tentativas com espera crescente. Alerta após 10 min (webhook ou caixa interna). Retomada automática com checkpoint. Lacuna registrada e nunca preenchida. | — | Unitários passam. **Teste MySQL de lock, reinício e outbox passou nesta review.** Gravação de 2,2 s no ciclo local e de 16–19 s em 2 ciclos na Aiven. | Meta de 99% dos ciclos em até 60 s sem medição contínua. Falta um supervisor do processo (systemd ou Agendador) no ambiente do time. | 🟡 |
| **US22** Mapa ao vivo | `GET /posicoes/atuais` traz a última posição de cada veículo, o horário GPS e o da coleta, `recente` (≤ 60 s) e filtros `linha` e `linha_gtfs`. Não chama a SPTrans. | `mapa.html` consulta a API a cada 30 s. Ônibus com mais de 60 s aparecem em âmbar. Há estados de carregamento, vazio e erro, com nova tentativa. Filtro por letreiro e por cor. | 31 testes do frontend passam. **No Chrome headless:** 11.283 ônibus reais, todos dentro do município. Com a API desligada, aparece o erro "Não foi possível carregar o mapa", sem dados fictícios. | **Bug corrigido nesta review:** o filtro de linha estava sempre "indisponível", porque `servidor.py` devolvia 404 para `data/linhas-sptrans.json`. A defasagem pode passar de 60 s (ver P1-2). Sem coletor contínuo, só há posições antigas. | 🟡 |
| **US13** Lacunas e versões | `GET /qualidade` traz início, fim e causa de cada lacuna, janelas de 15 min e GTFS do dia. | Aba "Qualidade da coleta". | Testes passam. A aba renderizou com dados reais (1 evento, "Sem versão vigente" na base local). | Herda a ambiguidade de vigência da US02: com 2 ou mais versões GTFS sem `feed_info`, o dia mostra ambiguidade em vez da versão vigente. | 🟡 |
| **US05** Acervo oficial | CLI de catalogação. PDF original no FileStore com data, órgão e tipo. `GET /documentos` filtra por tipo e data. | Aba "Acervo oficial" com visualizador de PDF. | Testes passam (só dados sintéticos). | **A tabela `documentos_oficiais` não existe na Aiven** (migração `us05_0014` não aplicada), então `/api/documentos` responde 503 lá. Nenhum contrato real foi catalogado. Não há rotina que garanta catalogar um documento novo em até 30 dias. | 🟡 |
| **US09** Parquet diário | Exportação atômica do dia D com fonte, licença CC BY 4.0 e período. Catálogo, dicionário e publicação D+1 às 06:00 via `python -m app.tarefas`. | Página `brutos.html`. | **Nesta review:** os 11.283 registros reais foram exportados, com metadados de fonte e licença conferidos. | O prazo das 06:00 depende de um Agendador do Windows num computador pessoal. A primeira publicação real atrasou, e nenhuma madrugada automática foi observada. | 🟡 |
| **US32** Backup e restauração | Backup lógico com hashes e restauração isolada (`marrom_restore_*`). Ensaio semestral agendado. | — | Restauração real em 20/11 levou 40,5 s (MySQL 8). Os testes MySQL desta review **não rodaram**: o código só aceita MySQL 8.x e havia apenas MariaDB. | Outro integrante ainda precisa reproduzir a restauração. É preciso confirmar se o destino do backup fica fora da máquina coletora ("local separado"). | 🟡 |

**Resultado:** 0 aprovadas, 10 parcialmente aprovadas, 1 não aprovada (US12).

## B. Avaliação do MVP

```
SPTrans ──► Coletor ──► MySQL ──► API Flask ──► Frontend ──► Mapa ao vivo
   ✅          ✅          ✅          ✅            ✅*           ✅*
```

| Etapa | Resultado | Evidência |
|---|---|---|
| SPTrans → Coletor | ✅ | 1 ciclo real com `GET /Posicao`: 11.283 veículos e 1 tentativa. O token fica só no backend. |
| Coletor → MySQL | ✅ | Execução `OK`, `confirmada_em` registrado, 2.200 ms de gravação. FK para `veiculos` íntegra e unicidade garantida no banco. |
| MySQL → API | ✅ | `/posicoes/atuais` respondeu 200 em 0,40 s (3,1 MB). `?linha=2450` trouxe 5 veículos. `?linha=abc` trouxe 400. CORS liberado para `127.0.0.1:5173`. |
| API → Frontend | ✅* | O frontend só usa HTTP: não há acesso a MySQL nem token. *O filtro de linha falhava (404 no catálogo) e foi corrigido. |
| Frontend → Mapa | ✅* | 11.283 marcadores reais. Posição com mais de 60 s aparece em âmbar. Estados de vazio e erro testados. Captura em [`docs/evidencias/sprint1-mvp-mapa.jpg`](../evidencias/sprint1-mvp-mapa.jpg). |
| GPS ↔ GTFS no mapa | ➖ | Aparece só quando a US03 foi processada (`linha.gtfs` nulo caso contrário). Não inventa associações. O filtro do frontend usa um catálogo estático, que hoje cobre 99,2% dos veículos (11.194 de 11.283). |

**Na base compartilhada (Aiven):** a revisão é `us29_0012`, enquanto o head da
`main` é `us11_0016`. Um teste numa base local na mesma revisão mostrou:
`/posicoes/atuais`, `/api/qualidade` e `/api/downloads/brutos/datas` → 200;
`/api/documentos` e `/indicadores` → **503**. Ou seja, o mapa funciona sobre a
Aiven, mas os dados de lá param em 23/11 às 14:01 UTC.

**Respostas objetivas**

1. **O backend funciona?** Sim. São 536 testes passando, e a API rodou com dados reais.
2. **O frontend funciona?** Sim. São 31 testes passando, e as 4 telas renderizaram com o backend real.
3. **Frontend e backend estão integrados?** Sim, por HTTP, com CORS conferido nas rotas `/api/*` e `/posicoes/atuais`.
4. **Os dados GPS chegam à persistência?** Sim. Foi comprovado com um ciclo real, numa base isolada.
5. **A API fornece informações reais para o mapa?** Sim. Ela lê os brutos gravados e nunca usa dados simulados.
6. **O mapa consegue apresentar e filtrar veículos?** Sim, depois da correção do `servidor.py`. Antes dela, o filtro ficava indisponível.
7. **O fluxo funciona sem dados fictícios?** Sim. Com a API fora do ar, o mapa mostra erro, não dados de exemplo.
8. **O que impede uma demonstração real?** (a) não há coletor contínuo rodando; (b) a branch com a correção ainda não foi integrada; (c) se a demonstração usar a Aiven, as abas Acervo e Indicadores respondem 503 até aplicar as migrações.

**Classificação do MVP: PRONTO.** Ele já foi demonstrado de ponta a ponta com
dados reais em ambiente isolado. Isso não significa produção operacional nem
histórias aceitas.

## C. Correções realizadas

| Repositório | Arquivo | Problema | Correção | Teste |
|---|---|---|---|---|
| Frontend | `servidor.py` | O servidor oficial (`python servidor.py`) só servia `.geojson` em `data/`. O catálogo `linhas-sptrans.json` recebia 404, e o filtro por linha do mapa (critério da US22) ficava "indisponível". | `.json` foi permitido em `data/`. A proteção contra travessia de caminho foi mantida. | Manual: o catálogo passou a responder 200, enquanto `/data/../servidor.py`, `/data/x.json` e `/.git/config` continuam em 404. No headless, o filtro aparece e "Filtro de linhas indisponível" fica oculto. Os 31 testes do frontend passam. |
| Backend | `app/__init__.py` | O Flask-Cors 6 (versão não fixada em `requirements.txt`) compara padrões sem regex de forma literal. Por isso `/qualidade/correspondencias`, `/documentos/<id>/arquivo` e `/downloads/brutos*` ficavam sem CORS. O frontend usa `/api/*` e não era afetado, mas as rotas públicas documentadas eram. | Os três padrões viraram regex que cobrem as sub-rotas. | Novo `test_cors_cobre_subrotas_publicas` (5 casos): 3 falham na `main` e todos passam com a correção. A suíte completa tem **536 passados, 9 ignorados**. |
| Backend | `docs/reviews/sprint1-mvp.md`, `docs/evidencias/sprint1-mvp-mapa.jpg` | — | Este relatório e a captura do mapa com dados reais. | — |

### Testes executados

| Conjunto | Resultado |
|---|---|
| Backend `pytest` (SQLite, isolado da Aiven) | 536 passados, 9 ignorados (MySQL opt-in e SPTrans real) |
| Frontend `node --test tests/` | 31 passados |
| Alembic | Head único `us11_0016`. `flask db upgrade` aplicado do zero até o head no MariaDB local. |
| MySQL US29 (lock, reinício, outbox) | Passou (MariaDB local) |
| MySQL US03 (espacial, migração) e US12 (cadeia) | Passaram. Duas falhas iniciais vieram de diferenças do MariaDB: erro 1020 do `innodb_snapshot_isolation` e código de CHECK 4025, que no MySQL é 3819. Não são bugs do projeto. |
| MySQL US32 | Não executado. O código exige MySQL 8.x, e o ambiente não tinha MySQL 8 nem Docker disponível. |
| Ponta a ponta | 1 ciclo real da SPTrans → MariaDB → Flask → `servidor.py` → Chrome headless |

## D. Pendências priorizadas

### P0 — impedem o MVP ou comprometem dados
1. **Integrar `review/sprint1-mvp` do frontend.** Sem ela, o filtro por linha
   não funciona no servidor oficial.
2. **Coletor contínuo não está em execução** no ambiente da equipe. Na Aiven,
   o último ciclo foi em 23/11 às 14:01 UTC. Sem ele, o mapa só mostra
   posições antigas. Decidir onde roda `python -m app.coleta.bus_collector` e
   colocá-lo sob supervisão (systemd `Restart=always` ou Agendador).

### P1 — impedem o aceite de requisitos importantes
1. **Aiven atrasada em 4 migrações** (`us09_0013` → `us11_0016`). A US05
   responde 503 e a US11 não funciona lá. Quem administra o banco deve fazer
   o backup (US32) antes e depois aplicar `flask db upgrade`.
2. **Defasagem de até 60 s (US22) não é garantida.** O pior caso é a soma do
   intervalo de coleta (30 s), do tempo de gravação (2 s local, **16–19 s
   medidos na Aiven**) e do polling do frontend (30 s), o que dá cerca de 62 s
   local e 78 s na Aiven. O mapa sinaliza honestamente em âmbar, mas o
   critério não é cumprido. Opções para a equipe: polling de 10–15 s,
   reduzir a latência de escrita (consulta de deduplicação `IN` com cerca de
   11 mil chaves) e compressão da resposta de 3 MB.
3. **US02: regra de vigência sobreposta** (decisão do PO) e importação da rede
   no agendador. Bloqueia também a US13 a partir da 2ª versão GTFS.
4. **US03: o processamento precisa rodar automaticamente** após a coleta, e
   o PO precisa validar a correspondência derivada.
5. **US01/US29: medir 99% dos ciclos num mês** de operação contínua (depende do P0-2).
6. **Brutos incompletos (US12):** a resposta `/Posicao` perde `c` (letreiro),
   `lt0`, `lt1`, `qv` e `hr`. Esses dados não podem ser recuperados depois.
   Guardá-los exige uma decisão de esquema (migração).
7. **US04:** sem fonte oficial disponível e sem partidas na Aiven. Falta o
   aceite humano da amostra.
8. **US05:** catalogar documentos reais e definir a rotina de 30 dias.
9. **US09/US32:** o agendamento depende de um PC pessoal. Falta observar a
   publicação automática das 06:00, a reprodução da restauração por outro
   integrante e confirmar que o backup fica em local separado.
10. **US12:** o critério oficial depende dos indicadores (US14+). O PO deve
    decidir se a história continua aberta ou é replanejada.
11. **Coleta de garagem** (nota do planejamento): decisão do PO.

### P2 — melhorias ou validações posteriores
- Fixar versões em `requirements.txt`. A mudança do Flask-Cors 6 já causou o
  bug corrigido aqui.
- O mapa usa `src/config.js` enquanto as outras telas usam `<meta api-base>`,
  o que cria dois pontos de configuração.
- O catálogo `data/linhas-sptrans.json` é estático (de 19/11) e cobre 99,2%
  hoje. Ele precisa ser regenerado periodicamente ou vir da API.
- A mensagem de vazio diz "para este filtro" mesmo sem filtro.
- `recente` usa a hora da coleta. Uma posição cujo GPS é até 10 min mais
  antigo aparece como recente: no ciclo real, 104 GPS tinham mais de 60 s e
  11 mais de 5 min. O PO deve decidir se isso deve ser sinalizado.
- A CLI `python -m app.exportacoes --data <hoje>` publica um dia incompleto
  com o nome final. O agendador só exporta D-1.
- A rota `GET /api/exemplo` é um stub de aprendizado e pode ser removida.
- Rodar os testes US32, US03 e US12 em MySQL 8 isolado, não só em MariaDB.
- Branches obsoletas: `us02-t02a-e--us02-t02b` e `fix/cadeia-migracoes-alembic`,
  esta última com um worktree "prunable" em `/tmp`. A `main` já tem a cadeia
  correta. Nenhuma outra branch remota tem commits fora da `main` (backend e
  frontend, exceto `origin/exemplo`, que é o protótipo com dados fictícios).

## E. Próximos passos (menor sequência para o MVP funcional)

1. Revisar e integrar `review/sprint1-mvp` no frontend (P0-1) e no backend (CORS).
2. Escolher a máquina e a base da demonstração. Para a Aiven, fazer o backup
   e aplicar as 4 migrações (P1-1). Para base própria, `flask db upgrade`.
3. Iniciar o coletor contínuo sob supervisão (P0-2) e confirmar com
   `python -m app.monitoramento metricas` que os ciclos gravam em até 60 s.
4. Iniciar `python app.py` e `python servidor.py` e abrir o mapa. Conferir o
   contador, o filtro por linha e a cor verde/âmbar.
5. Ajustar o polling ou a latência para fechar os 60 s (P1-2).
6. Depois da demonstração: US03 automática, regra de vigência da US02 e a
   medição mensal US01/US29.

## F. Parecer final

- **A Sprint 1 pode ser aceita?** A **meta** foi atingida e comprovada com
  dados reais, mas o **backlog** não: 0 de 11 histórias cumprem
  integralmente os critérios oficiais (10 parciais, US12 não aprovada).
- **O MVP está pronto para demonstração?** **Sim (PRONTO)**, com a correção
  do `servidor.py` integrada e o coletor rodando durante a apresentação.
- **Quais US ainda não podem ser aprovadas?** Todas. As que estão mais
  próximas, porque só falta comprovação operacional ou uma pendência
  pequena, são US22, US13, US29, US03 e US01. As mais distantes são US12,
  US04 e US05.
- **O que é obrigatório antes de apresentar?** (1) integrar a correção do
  filtro de linha; (2) manter um coletor rodando contra a base da
  demonstração; (3) se a demonstração usar a Aiven com todas as abas, aplicar
  antes as migrações pendentes, com backup. Também é preciso informar que
  posições em âmbar indicam defasagem acima de 60 s, um limite ainda não
  garantido.

---

## Atualização — correções finais do MVP (branch `fix/sprint1-mvp`)

As correções da review foram levadas para `fix/sprint1-mvp` nos dois
repositórios e completadas com as alterações abaixo. Nada foi commitado.

| Problema | Correção | Evidência |
|---|---|---|
| P0-1: filtro de linha com 404 no catálogo | Mantida a correção do `servidor.py`. O servidor também envia `Cache-Control: no-cache`: no teste, o navegador reutilizou um `mapa.js` antigo. | `tests/test_servidor.py` falha no código original. No Chrome real, `189L-10` mostra 8 ônibus e "Todas" restaura 11.644. |
| P0-2: sem coletor contínuo | Nenhum defeito impeditivo encontrado. Documentados início, monitoramento e parada (`docs/COLETOR.md`). | O coletor rodou continuamente no MariaDB local. Duplicatas foram recusadas pelo checkpoint e pelo `GET_LOCK`, e o SIGTERM gerou lacuna `PROCESSO_INTERROMPIDO` encerrada na retomada. |
| P1-2: limite de 60 s | A deduplicação usa pares exatos no índice único, em lotes de 10 mil. Os `INSERT`s usam lotes de 2000. A API responde com ETag/304. O mapa consulta a cada 10 s, só reprocessa com ciclo novo e mostra a última coleta. | Ver medição abaixo. |
| P1-1: Aiven atrasada em 4 migrações | Procedimento de backup, aplicação, validação e rollback para o responsável (`docs/AIVEN-MIGRACOES-MVP.md`), com o SQL em `docs/sql/`. | Ensaio local com 390 mil brutos: upgrade, recusa de downgrade, rollback manual e re-upgrade. |
| Mapa: agrupamento inteiro âmbar por ~3% de GPS parado | Agrupamento misto fica verde com contorno âmbar; só fica âmbar se todas as posições forem antigas. | `classificarGrupo` testado. Captura com 26 grupos verdes e 1 âmbar. |
| Mapa: mensagem de vazio sem filtro | "Nenhuma posição disponível" quando a API não traz posições. | Base vazia em `us29_0012` no Chrome real. |

### Medição da latência (proxy local com o RTT de 218 ms medido na Aiven)

| Medida | `main` | `fix/sprint1-mvp` |
|---|---|---|
| Deduplicação na Aiven (somente leitura, ~8 mil chaves reais) | 5,75 s, 342 mil linhas examinadas | 1,81 s, 7.941 linhas examinadas |
| Gravação por ciclo (`duracao_gravacao_ms`) | 14,3–20,3 s | 8,7–9,9 s |
| Coleta até a tela, por ciclo | 18,4–20,3 s | 11,6–12,5 s (o primeiro valor, 21,4 s, é o snapshot inicial) |
| Defasagem exibida, máxima observada em 210 s | 46,5 s (com a fase favorável) | 39,7 s |
| Pior caso teórico (30 s de coleta + gravação + API + polling) | ≈ 30 + 20 + 30 ≈ 80 s | ≈ 30 + 12 + 10 ≈ 52 s |
| Bytes por consulta sem ciclo novo | ~3 MB (sempre 200) | 0 (304) |

A emulação reproduz o RTT, mas não a banda nem a carga da Aiven. **Os 60 s
não estão comprovados em operação real.** Para comprovar, rode
`python -m scripts.medir_defasagem_mapa` contra a implantação. Veículos cujo
GPS a SPTrans não atualizou mantêm o `coletado_em` da primeira gravação (a
US01 não repete `(prefixo, horário GPS)`) e continuam marcados como antigos,
de forma conservadora.

### Proposta para o PO: metadados da linha nos brutos (não implementada)

Todos os campos exigidos pela US01 são persistidos, e o objeto do veículo é
guardado inteiro (`p`, `a`, `ta`, `py`, `px`, `sv`, `is`). Da resposta
`/Posicao` ficam de fora `c` (letreiro), `lt0` e `lt1` (terminais), `qv` e o
`hr` da resposta. A proposta é guardar `c`, `lt0` e `lt1` em colunas novas e
anuláveis em `registros_gps`. Isso exige migração (`ADD COLUMN` instantâneo no
MySQL 8), ajuste do Parquet (US09) e do dicionário, e cerca de 40 bytes por
registro (algumas centenas de MB por mês). A decisão é do PO, junto com a
US12.
