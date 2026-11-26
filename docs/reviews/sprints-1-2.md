# Review geral — Sprints 1 e 2 (22 histórias)

Data: 26/11/2026 · Branch `review/sprints-1-2-integracao` nos dois repositórios, criada a
partir de `origin/main` (backend `113faba`, frontend `372d506`). Nada foi commitado.
A Aiven não foi acessada (nem para leitura). Fonte de verdade do aceite: critérios do
documento *Requisitos — Análise de dados do transporte público 02*.

**Resumo.** O código das duas sprints está coerente e matematicamente correto. Os valores
foram conferidos à mão em dados controlados, e o frontend consome a API sem
divergências de contrato. A review corrigiu três defeitos: a migração que quebrava
`db upgrade`, ônibus que sumiam do mapa ao vivo e o arredondamento que escondia avisos
de qualidade.

**O bloqueio principal continua aberto:** não existe rotina que reconstrua
`ViagemObservada` a partir do GPS (US14-T01C). Sem ela, nenhum indicador da Sprint 2
é produzido com dados reais. A Sprint 2 funciona de ponta a ponta somente a partir de
viagens sintéticas.

## 1. Matriz de aceite

Legenda: **APROVÁVEL** = critérios comprovados · **PARCIAL** = funciona, faltam critérios
ou evidências · **NÃO APROVÁVEL** = bloqueio relevante.

| US | Implementação | Integração | Testes | Pendências | Parecer |
|---|---|---|---|---|---|
| US01 Coleta 30 s | `BusCollector`/`SPTransClient`; intervalo 15–60 s validado; NULL preservado; `UNIQUE(prefixo_origem, horario_gps)`; processo separado com `GET_LOCK` | Fluxo A ok (demo) | `test_bus_collector.py`; MySQL US29 passou em MariaDB isolada | Meta de ≥ 99% dos ciclos no mês sem observação contínua; "coletar garagem" não implementado | PARCIAL |
| US02 GTFS | Captura diária em `app.tarefas`, SHA-256, ZIP original, vigência por `feed_info` | Versão vigente no relatório US13 | `test_gtfs*.py`, `gtfs/test_scheduler.py`, `test_vigencia.py` | Entrada em ≤ 24 h nunca observada em operação; ZIPs ficam no disco local (não são compartilhados pela Aiven); diário × semanal (PO) | PARCIAL |
| US12 Brutos | Brutos imutáveis; `observacoes_tratadas` separada; manifesto de reprocessamento | Parquet lê só brutos | `test_reprocessamento.py`, `test_reproducao_us12_t04.py` | "Recalcular um mês a partir dos brutos" não é possível: o recálculo parte de `viagens_observadas` (bloqueio US14-T01C) | PARCIAL |
| US04 Partidas | Oficial prioritária, fallback GTFS (calendário, > 24 h) | Usada por US14–US18 | `tests/programacao`; conferência de 30 linhas ([US04-T04](../US04-T04.md)) | Só roda por comando manual (fora de `app.tarefas`); pendências da US04-T04 | PARCIAL |
| US03 Correspondência | `MatchingService` versionado; não associados com motivo | Alimenta mapa (linha GTFS) e US26 | `test_correspondencias.py`; **real 19/11: 99,55%** ([evidência](../evidencias/US03-2026-11-19.json)) | Só roda manualmente (`python -m app.correspondencia processar`); sem relatório diário automático | PARCIAL |
| US29 Resiliência | 1 + 5 tentativas (1/2/4/8/16 s), alerta em 600 s, lacuna registrada, checkpoint | Lacunas no US13/US26 | `tests/resiliencia`; MySQL (lock/reinício/outbox) passou nesta review | ≥ 99% gravados em ≤ 60 s sem medição mensal (amostra: 2 ciclos, 16–19 s); falta supervisor de processo | PARCIAL |
| US22 Mapa ao vivo | `/posicoes/atuais`, `recente` ≤ 60 s, filtro de linha, ETag/304. **Corrigido:** ônibus deduplicado sumia | Verificado no navegador e por `conferir_integracao_api.mjs` | `tests/consultas`, `tests/mapa/*` | Defasagem ≤ 60 s só no pior caso teórico (≈ 52 s); sem medição contínua | PARCIAL |
| US13 Lacunas/versões | Relatório diário com início, fim e causa; GTFS vigente/ambígua; silêncios inferidos | Página *Qualidade da coleta*; validado pelo cliente | `tests/qualidade`, `qualidade.test.mjs` | — (real depende da operação contínua) | APROVÁVEL |
| US05 Acervo | Catálogo + PDF original (SHA-256) + busca por tipo/data | Página *Acervo* | `tests/documentos`, `acervo.test.mjs` | "Catalogado em até 30 dias" exige rotina de descoberta/registro; não há processo nem evidência | PARCIAL |
| US09 Parquet | Diário, atômico, dicionário, fonte, CC BY 4.0; publicado só se completo | Catálogo → download validado (`PAR1`) | `tests/exportacoes`; real 19/11: 170.648 linhas idênticas ([evidência](../evidencias/US09-T03-2026-11-20.json)) | Prazo 06:00 de D+1 depende de `app.tarefas` ativo; sem evidência contínua | PARCIAL |
| US32 Backup | Banco + originais + hashes + revisão Alembic; restauração só em base `marrom_restore_*` local | — | Restauração real em 40,5 s em MySQL 8 ([evidência](../evidencias/US32-restauracao-2026-11-20.json)); não reexecutável aqui (o serviço recusa MariaDB) | Agendamento diário depende de máquina Windows ligada; "local separado" e ensaio semestral são operacionais | PARCIAL |
| US14 Cumprimento | Fórmula, cobertura ≥ 80%, ±½ intervalo (máx. 15 min), NULL com denominador 0 — **corretos** | Painel/CSV ok com viagens sintéticas | `test_cumprimento.py` + 20 casos sintéticos (US14-T04) + 16 partidas conferidas à mão na demo | **Sem reconstrução de viagens (US14-T01C)**; aceite institucional pendente; API só publica janelas de 15 min | NÃO APROVÁVEL |
| US16 Pontualidade | −60…+300 s inclusivo; frequência excluída; NULL com motivo — **corretos** | Painel/CSV ok | `test_pontualidade*.py`; 20 casos (US16-T03) | Mesmo bloqueio; viagem com cobertura < 80% conta como realizada (decisão do PO); aceite institucional | NÃO APROVÁVEL |
| US15 Frequência | Interpolação só entre posições válidas consecutivas, sem recuo, uma passagem por parada; passagens/h; zero ≠ ausência | Mapa e `/indicadores?tipo=FREQUENCIA` lado a lado | `test_passagens*`, `test_frequencia*`; 20 casos US15-T04 conferem | Depende de `ViagemObservada`; "aceite dispensado pelo responsável" **não** atende o critério oficial | NÃO APROVÁVEL |
| US23 Painel | `/series` sem recálculo, filtros, lacunas, zero/NULL, versões; agenda de 15 min + consolidação 04:00–06:00 numa transação | Telas conferidas no navegador | `test_series_historicas.py`, `test_agenda_*`, `series*.test.mjs` | Sem indicadores reais (upstream); branch de revisão não integrada (seção D, P1-5) | PARCIAL |
| US17 Regularidade | CV populacional, > 0,5 estrito, NULL < 2 intervalos/média 0 | Painel e mapa | `test_regularidade_us17.py`; 20 casos conferem; CV 0,808122 conferido à mão | Bloqueio upstream; regra de 3 passagens por janela difere da letra do critério (E1); aceite institucional | NÃO APROVÁVEL |
| US18 Espera | Σh²/2Σh em min; excesso negativo preservado; NULL com motivo | Espera e excesso pareados no painel | `test_espera_us18.py`; 20 casos conferem; 3,857/−1,143 min conferidos | Bloqueio upstream; E1 e E3; aceite institucional | NÃO APROVÁVEL |
| US10 CSV | CSV diário após a consolidação; filtros linha/sentido/período; unidade, fonte, licença | Download pelo histórico | `test_indicadores_csv.py`, `series-exportacao.test.mjs` | Formato pouco amigável a planilha pt-BR (E8); prazo depende da operação | PARCIAL |
| US11 API pública | Sem login, `/docs/indicadores`, 400 com campo, 60/min por IP persistido | Verificado por HTTP real | `tests/api/*`; p95 61 ms em ensaio sintético local | 95% < 2 s em operação real não medido | PARCIAL |
| US24 Mapa de indicadores | Parada, trecho, distrito, subprefeitura (GeoSampa versionado); faixas e legenda; zero ≠ NULL | Validado pelo cliente; 5 paradas em Sé/Brás/Mooca | `mapa-indicadores.test.mjs`, `mapa-conferencia.test.mjs` | Depende da US15 real; **distrito/subprefeitura só agregam `%`**, e os indicadores por parada (frequência, regularidade, espera) não são `%` → agregação territorial sempre `AGREGACAO_NAO_SUPORTADA` (P1-7, E10) | PARCIAL |
| US25 Exportação | CSV da versão exibida + SVG com data, fonte e versão | **CSV = painel** (valores e versão) em 3 indicadores | `series-painel-exportacao.test.mjs`, `test_*us25*` | — | APROVÁVEL |
| US26 Aviso | Falhas ÷ ciclos esperados; exatamente 10% sem aviso; NULL com motivo; ausência operacional separada | Painel, mapa, CSV e API mostram o mesmo bloco | `tests/qualidade/*us26*`; demo: 16,67% avisa, 10% não, 66,67% avisa | — | APROVÁVEL |

## 2. Correções realizadas (A)

| Arquivo | Problema | Solução |
|---|---|---|
| `migrations/versions/merge_0023_us06_us07.py` (novo) | **P0:** duas cabeças Alembic (`us06_0021`, `us07_0022`) desde o merge da US07; `flask db upgrade` para com "Multiple head revisions" | Merge sem DDL (as tabelas são disjuntas). Cadeia inteira aplicada em MariaDB isolada; `db check` sem divergências |
| `app/consultas/posicoes.py` | **P1 US22:** o modo `ultima_coleta` lia só o último ciclo. Cada bruto pertence ao ciclo que o gravou primeiro, então um ônibus com GPS inalterado sumia do mapa | Lê a faixa `[último ciclo − 60 s, último ciclo]` no índice `coletado_em` e mantém o registro mais recente por veículo |
| `app/qualidade/service.py` | **P1 US13/US26:** `INCOMPLETA` era decidido sobre o percentual arredondado (10,004% → 10,00% → sem aviso), diferente da regra exata da US26 | Mesma comparação inteira `falhas × 100 > 10 × denominador` |
| `tests/gtfs/test_scheduler.py`, `tests/exportacoes/test_downloads_brutos.py` | Testes desatualizados após o merge da US07 (cabeça e tarefas periódicas) | Atualizados para a cadeia e a lista de tarefas atuais |
| `tests/exportacoes/test_indicadores_csv.py` | O teste dependia do relógio real: passou a falhar a partir de 26/11 06:00 | Relógio controlado; cobre os casos antes e depois do prazo |
| `tests/consultas/test_posicoes_atuais.py`, `tests/qualidade/test_relatorio_diario_us13.py` | Faltavam os limites corrigidos | Casos do ônibus deduplicado e da fronteira de 10% |
| `scripts/demo_sprints_1_2.py` (novo) | Não havia como o PO validar a Sprint 2 de ponta a ponta | Banco descartável que passa pelo pipeline real (seção 4) |
| Frontend `scripts/conferir_integracao_api.mjs` (novo) | Os testes do frontend usavam apenas fixtures | Verificação contra a API real com os clientes e validadores das páginas |
| READMEs | Intervalo do mapa citado como 10 s e 30 s; sem instrução de isolamento dos testes | Corrigido para 10 s; seções de demonstração e testes seguros |

## 3. Testes (B)

| Suíte | Resultado |
|---|---|
| Backend `pytest` (SQLite) | **978 passaram, 0 falharam, 10 ignorados** (opt-in de MySQL/SPTrans real). Linha de base antes da review: 972 / 3 falhas |
| Backend MySQL opt-in em **MariaDB 13 isolada** (127.0.0.1:3399) | `db upgrade` até `merge_0023` e `db check` ok; US12 base vazia ok; US29 (lock, reinício, outbox) ok. US12 com histórico: a `CHECK` foi aplicada, mas MariaDB devolve o código 4025 em vez do 3819 do MySQL (diferença de dialeto). US32: o serviço recusa servidor que não seja MySQL 8 (salvaguarda correta) |
| Frontend `node --test tests/` | **169/169**; `tests/test_servidor.py` 3/3 |
| Cadernos US14-T04, US15-T04, US17-T03B, US18-T03B | 20/20 cada (sintéticos) |
| Integração real (`conferir_integracao_api.mjs`) | **12/12** |
| Visual (Chrome headless) | Histórico (cumprimento, regularidade, espera), mapa de indicadores, mapa ao vivo, qualidade, brutos, acervo |

Limitações: não foi possível usar MySQL 8 real (sem permissão no Docker). Nenhum teste
usou dados reais da SPTrans, e nada comprova operação contínua.

## 4. Integração ponta a ponta (C)

| Fluxo | Resultado |
|---|---|
| A — SPTrans → coleta → MySQL → API → mapa | Coleta → API → mapa ok (demo e testes). SPTrans real não exercitada nesta review |
| B — GPS + GTFS + programação → correspondência → viagens → passagens → indicadores → API → painéis | **Interrompido em "viagens"** (US14-T01C). Correspondência e partidas são manuais. A partir de `ViagemObservada`, o pipeline real funciona e os valores conferem com o cálculo manual |
| C — indicadores → conjunto publicado → séries → CSV/imagem → versão preservada | Ok: consolidação atômica; o CSV da `versao_dados` exibida é idêntico ao painel |
| D — ciclos/lacunas → qualidade → indicadores/API → gráficos/mapas/exportações | Ok: o mesmo bloco `falhas_coleta` aparece na API, no painel, no mapa e no CSV; fronteiras de 10% corretas |
| E — brutos → Parquet → catálogo → download | Ok: publicação atômica, dicionário, licença; verificação `PAR1` |

Sem duplicação de cálculo: o frontend não recalcula nenhum indicador. Credenciais e
MySQL ficam somente no backend.

## 5. Pendências (D)

| ID | Pendência | Impacto | Menor ação |
|---|---|---|---|
| **P0-1** | Não há `TripReconstructionService` (US14-T01C) que gere `ViagemObservada` a partir de `observacoes_tratadas` + partidas | US14–US18, US23 e US24 sem dados reais; US12 sem reprodução mensal | PO decide E6; implementar um serviço em `app/viagens/` que use `SeparadorViagens` e `Percurso`, chamado pela agenda antes dos indicadores |
| P1-1 | Correspondência (US03) e importação de partidas (US04) só manuais | Sem tratadas e sem partidas do dia em operação | Tarefas periódicas em `app.tarefas` com lote e janela definidos |
| P1-2 | Coletor e tarefas sem supervisor de processo; nenhuma operação contínua | Sem evidência de 99%/mês, de 60 s, do prazo de 06:00 e de backup diário | Servidor sempre ligado (systemd `Restart=always`) com coletor e `app.tarefas` |
| P1-3 | Nenhum dos 20 casos de US14–US18 foi aprovado pela Instituição Parceira | Critério comum US14–US19 não atendido | Enviar os cadernos (já com campos em branco para a instituição) e registrar a resposta |
| P1-4 | API/painel publicam cumprimento e pontualidade só em janelas de 15 min | Faixa/dia/mês só nos cadernos | Agregação por soma de contagens (padrão da US15-T03) em `/indicadores` |
| P1-5 | Branch `Review--US16-T03…US23-T01-` não integrada: recorta o período efetivo de `/series` ao histórico | Lacunas declaradas antes da primeira coleta | Revisar e integrar por PR (conflita com `series.py`) |
| P1-7 | Agregação territorial (US24) só para `%`; nenhum indicador por parada é `%` | Distrito/subprefeitura sem valor com dados do pipeline | Decisão E10 e regra por soma de contagens ÷ soma de horas |
| P1-6 | Aiven precisa de `flask db upgrade` até `merge_0023` (inclui US06/US07) | Tabelas de clima/infra ausentes | Responsável pelo banco: backup + upgrade |
| P2-1 | CSV `,`/`.` e horários em UTC | Uso em planilha pt-BR | Decisão E8 |
| P2-2 | Rodapé fixo "Fonte: SPTrans Olho Vivo"; indicadores calculados não herdam `origem: SINTETICA` | Demonstração pode parecer real | Propagar a origem da viagem para `qualidade` |
| P2-3 | `__pycache__/*.pyc` versionados no frontend | Ruído | `git rm --cached` + `.gitignore` |
| P2-4 | Sem MySQL 8 no ambiente local de QA | Restauração e códigos de erro não reverificados | Permissão no Docker ou instância MySQL 8 local |

## 6. Decisões do PO (E)

1. **Três passagens por janela (US17/US18).** Hoje cada intervalo pertence à janela da
   passagem posterior: na demo, a janela 07:30 tem só 2 passagens e recebe CV. (a) Literal:
   exigir 3 passagens dentro da janela. (b) Manter: exigir 2 intervalos, permitindo uma
   passagem da janela anterior.
2. **Desvio padrão (US17):** populacional (atual) ou amostral.
3. **Excesso com faixas diferentes (US18):** `ΣH²/2ΣH` dos intervalos vigentes (atual) ou
   `H/2` da faixa do início da janela.
4. **Pontualidade (US16):** viagem com cobertura < 80% conta como realizada (atual,
   sinalizada) ou é excluída.
5. **Tolerância de horário fixo (US14):** menor intervalo até a partida vizinha (atual,
   provisório) ou intervalo da faixa programada.
6. **Reconstrução (P0-1):** o que é saída observada (passagem estimada na 1ª parada?), como
   medir a cobertura (fração dos trechos com passagem?) e como parear (partida mais próxima
   dentro da tolerância, um para um?).
7. **Cadência:** GTFS e partidas diárias (oficial) ou semanais (nota de priorização);
   Parquet diário (oficial) ou semanal/mensal.
8. **CSV (US10):** manter `,`/`.` em UTC ou gerar `;`/`,` em horário de São Paulo.
9. **Coleta de garagem (nota de priorização da US01):** incluir ou não.
10. **Agregação territorial (US24):** frequência por distrito = Σ passagens ÷ Σ horas das
    paradas (passagens/h por parada, média ponderada) ou não agregar não percentuais
    (atual: `AGREGACAO_NAO_SUPORTADA`).

## 7. Validação externa (F)

Dependem de operação real ou de terceiros: ≥ 99% dos ciclos no mês (US01), entrada do
GTFS em ≤ 24 h (US02), ≥ 98% de associação contínua (US03), conferência com a
programação oficial (US04), catalogação em ≤ 30 dias (US05), prazos de 06:00 (US09, US10,
US23), ≤ 60 s de gravação em 99% dos ciclos (US29), defasagem ≤ 60 s medida (US22),
95% < 2 s em operação (US11), backup diário e ensaio semestral (US32), e aceite da
Instituição Parceira (US14–US18).

## 8. Próximos passos (G)

1. Revisar e commitar esta branch nos dois repositórios (PRs separados).
2. PO decide E1–E6; implementar a reconstrução de viagens (P0-1) com 20 casos manuais.
3. Agendar correspondência e partidas (P1-1); subir coletor e tarefas sob supervisor (P1-2).
4. Aplicar `merge_0023` na Aiven (P1-6) e coletar uma semana real; medir US01/US22/US29.
5. Enviar os cadernos à Instituição Parceira (P1-3).

## 9. Parecer geral (H)

1. **Sprint 1 integralmente?** Não. A funcionalidade está pronta e US13 é aprovável. As
   demais dependem de operação contínua, de agendamento (US03/US04) ou de evidência
   mensal.
2. **Sprint 2 integralmente?** Não. US25 e US26 são aprováveis. US14–US18 estão bloqueadas
   pela falta de viagens reconstruídas e de aceite institucional.
3. **MVP anterior funcional?** Sim. A coleta, o mapa (corrigido), a qualidade, o acervo e os
   brutos continuam funcionando, e a suíte inteira passa.
4. **Indicadores confiáveis?** As fórmulas sim: foram conferidas à mão e nos cadernos. Os
   dados ainda não, porque não há viagens reais.
5. **Frontend e backend integrados?** Sim: 12/12 verificações com a API real e telas
   conferidas.
6. **Bloqueios:** US14, US15, US16, US17 e US18 (P0-1, P1-3).
7. **Obrigatório antes da demonstração:** aplicar esta branch (a migração `merge_0023`
   e a correção do mapa) e demonstrar a Sprint 2 com o banco sintético, declarado como
   tal.
8. **Avançar para a Sprint 3?** Sim, desde que P0-1 entre como primeiro item: features
   novas sobre indicadores sem viagens reais só aumentariam o que não pode ser validado.

## 10. Como o PO valida

**Iniciar (sem Aiven):**

```bash
# backend
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python -m scripts.demo_sprints_1_2 --pasta /tmp/demo-marrom          # pasta vazia
python -m scripts.demo_sprints_1_2 --pasta /tmp/demo-marrom --servir # API :5000
# frontend (outro terminal)
python servidor.py            # http://127.0.0.1:5173
node scripts/conferir_integracao_api.mjs   # esperado: 12/12
```

A demonstração é **sintética**: linha `DEMO-01` com 5 paradas a leste da Praça da Sé, dia
24/11/2026. A ida tem horário fixo a cada 15 min e a volta, frequência de 10 min, das 06:00
às 09:59. As viagens são inseridas pelo script, porque falta a P0-1. Passagens, indicadores,
consolidação, CSV e Parquet saem do código real. Os limites territoriais são o snapshot
oficial GeoSampa. As posições do mapa ao vivo são de "agora" e ficam âmbar após 60 s.

**Coletor real (somente em ambiente autorizado, com `.env` próprio):**
`python -m app.coleta.bus_collector --ciclos 2` para uma amostra, ou sem `--ciclos` para
coleta contínua. `Ctrl+C` para. Tarefas: `python -m app.tarefas`. Nunca rode contra a base
compartilhada sem combinar com o responsável.

**O que conferir (horário de São Paulo):**

| Tela / endpoint | Filtro | Esperado |
|---|---|---|
| Histórico por linha | DEMO-01, ida, Cumprimento, 24/11 | 06:00 100% (+30 s); 06:15 100% (−90 s ≤ 450 s); 07:15 **0%** (cobertura 0,7); 08:00 **0%** (+700 s); 08:15 0% (sem viagem); antes das 06:00 "sem resultado" (nenhuma partida) |
| Histórico por linha | ida, Pontualidade | 06:15 0% (−90 s); 06:45 0% (+400 s); 07:15 100% (+60 s); 08:15 sem resultado; 08:45 0% (+310 s); 09:00 0% (−61 s) |
| Avisos (qualquer gráfico) | — | 06:30 **16,67%** com aviso; 07:00 **10%** sem aviso; 08:00 **66,67%** com aviso e causa `COLETOR_PARADO_DEMO` |
| Histórico por linha | volta, Regularidade, parada D, 08:00–09:00 | 08:30 CV **0,808** com agrupamento; demais janelas sem resultado, com motivo |
| Histórico por linha | volta, Espera, parada D, 08:00–09:00 | 08:30 espera **3,86 min**, excesso **−1,14 min** |
| Mapa de indicadores | Paradas, Frequência observada, volta, 24/11 08:30–08:45 | Paradas A–E com 12 passagens/h (programada na origem E: 8/h); escopo Distrito: Sé, Brás e Mooca aparecem sem valor, com "agregação não suportada" (P1-7); demais distritos "sem dados" |
| Exportar a versão exibida | qualquer painel | O CSV tem a mesma versão (`X-Versao-Dados`) e os mesmos valores da tela |
| Qualidade da coleta | 24/11 | Lacuna 08:00–08:10 registrada; GTFS vigente |
| Dados brutos / Acervo | — | Parquet de 24/11 (911 registros); 1 documento SINTÉTICO |
| Ônibus ao vivo | — | `DEMO-LIVE-1` e `DEMO-LIVE-2`; o segundo só existe no ciclo anterior e continua visível |
| `GET /docs/indicadores`, `GET /indicadores?sentido=x` | — | Guia 200; erro 400 com `campo` e `X-RateLimit-Limit: 60` |

**Funciona com dados reais hoje:** coleta, brutos, Parquet, qualidade, mapa ao vivo,
acervo e correspondência (manual). **Depende de dados sintéticos:** todos os indicadores
da Sprint 2.

**Reproduzir os testes:**

```bash
DATABASE_URL=sqlite:// SPTRANS_TOKEN= python -m pytest -q   # backend
node --test tests/                                          # frontend
```
