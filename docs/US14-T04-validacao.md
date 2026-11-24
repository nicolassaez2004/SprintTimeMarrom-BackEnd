# US14-T04 — Validação de 20 casos de cumprimento de viagens

**Situação: PARCIAL — conferência técnica concluída; validação pela Instituição Parceira pendente.**
Preparado em 24/11/2026 na branch `review/us14-us16-indicadores`. Nenhum dado
desta página foi aprovado pela Instituição Parceira.

## Regra conferida (critério oficial da US14)

- Cumprimento = viagens cumpridas ÷ partidas programadas × 100, duas casas.
- Viagem cumprida: cobertura do traçado **≥ 80%** e saída observada dentro de
  **± metade do intervalo programado, limitada a ± 15 min** (limites inclusivos).
- Frequência: intervalo = `headway` da programação. Horário fixo: menor
  intervalo até a partida vizinha na grade completa do dia operacional
  (**regra técnica provisória, pendente de decisão do PO**).
- Sem partidas programadas na janela: contagens 0/0 e valor **NULL**.
- Janela = partidas cuja **saída prevista** está em `[início, fim)`.
  Agregações somam contagens; não fazem média de percentuais.

## Natureza dos dados

Os 20 casos usam uma linha **sintética** (`VAL-10`), criada para exercitar
cada fronteira da regra. Não são programação nem viagens reais da SPTrans.
O cálculo manual foi feito à mão e gravado como valor fixo no teste; o
resultado do sistema vem do fluxo persistido completo (programação → viagem
observada → `IndicadorOperacional`) em banco SQLite isolado.

Grade de referência (horário de São Paulo, programação OFICIAL, dia útil):

| Dia operacional | Sentido | Oferta | Partidas |
|---|---|---|---|
| 24/11/2026 | 0 | fixa | 05:00, 05:20, 05:40, 06:00, 06:10, 06:20, 06:30, 07:00, 08:00, 23:30, 24:10 (= 00:10 de 25/11) |
| 24/11/2026 | 1 | fixa | 06:00, 06:30 |
| 25/11/2026 | 0 | fixa | 05:00, 05:30 (há também uma captura GTFS mais recente com 05:00, 05:15, 05:30, que deve ser ignorada) |
| 26/11/2026 | 0 | frequência (600 s) | 06:00 a 06:50, a cada 10 min |

Viagens observadas: versão de reconstrução `recon-val-1`, correspondência
`corr-val-1`. Existe uma viagem de outra versão (`recon-val-0`) para 24/11
06:20, que não deve contar.

## Casos (manual × sistema)

Reproduzir: `DATABASE_URL=sqlite:// python -m tests.indicadores.test_validacao_us14_t04 --csv`
(gera esta tabela e a planilha) e `python -m pytest tests/indicadores/test_validacao_us14_t04.py`.
Um teste falha se esta tabela ou a planilha divergirem do cálculo atual.

Planilha para conferência da Instituição Parceira:
[`evidencias/US14-T04-casos.csv`](evidencias/US14-T04-casos.csv). As colunas
`conferencia_instituicao` e `observacao_instituicao` estão em branco de propósito.

| # | Janela | Sentido | Período (horário de São Paulo) | Dia(s) operacional(is) da grade | Programação de referência (partida: atraso s / cobertura) | Cálculo manual | Sistema | Confere |
|---|---|---|---|---|---|---|---|---|
| 1 | 15 min | 0 | 24/11 05:00 – 24/11 05:15 | 2026-11-24 | 24/11 05:00: +600 s / 80% | 1/1 = 100.00<br><small>05:00 intervalo 20 min -> tol 600 s; +600 s e cobertura 80% -> cumprida</small> | 1/1 = 100.00 | sim |
| 2 | 15 min | 0 | 24/11 05:15 – 24/11 05:30 | 2026-11-24 | 24/11 05:20: +0 s / 79% | 0/1 = 0.00<br><small>05:20 cobertura 79% < 80% -> não cumprida</small> | 0/1 = 0.00 | sim |
| 3 | 15 min | 0 | 24/11 05:30 – 24/11 05:45 | 2026-11-24 | 24/11 05:40: -601 s / 100% | 0/1 = 0.00<br><small>05:40 intervalo 20 min -> tol 600 s; -601 s -> não cumprida</small> | 0/1 = 0.00 | sim |
| 4 | 15 min | 0 | 24/11 06:00 – 24/11 06:15 | 2026-11-24 | 24/11 06:00: +300 s / 90%<br>24/11 06:10: +301 s / 90% | 1/2 = 50.00<br><small>06:00 e 06:10 intervalo 10 min -> tol 300 s; +300 s sim, +301 s não</small> | 1/2 = 50.00 | sim |
| 5 | 15 min | 0 | 24/11 06:15 – 24/11 06:30 | 2026-11-24 | 24/11 06:20: sem viagem | 0/1 = 0.00<br><small>06:20 sem viagem na versão recon-val-1 (só em outra versão) -> não cumprida</small> | 0/1 = 0.00 | sim |
| 6 | 15 min | 0 | 24/11 06:30 – 24/11 06:45 | 2026-11-24 | 24/11 06:30: -300 s / 85% | 1/1 = 100.00<br><small>06:30 intervalo 10 min -> tol 300 s; -300 s -> cumprida</small> | 1/1 = 100.00 | sim |
| 7 | 15 min | 0 | 24/11 07:00 – 24/11 07:15 | 2026-11-24 | 24/11 07:00: +900 s / 100% | 1/1 = 100.00<br><small>07:00 intervalo 30 min -> tol 900 s; +900 s -> cumprida</small> | 1/1 = 100.00 | sim |
| 8 | 15 min | 0 | 24/11 08:00 – 24/11 08:15 | 2026-11-24 | 24/11 08:00: +901 s / 100% | 0/1 = 0.00<br><small>08:00 intervalo 60 min -> metade 1800 s limitada a 900 s; +901 s -> não</small> | 0/1 = 0.00 | sim |
| 9 | 15 min | 0 | 24/11 10:00 – 24/11 10:15 | — | — | 0/0 = NULL<br><small>nenhuma partida programada -> denominador 0, valor NULL</small> | 0/0 = NULL | sim |
| 10 | 15 min | 0 | 24/11 23:30 – 24/11 23:45 | 2026-11-24 | 24/11 23:30: -900 s / 95% | 1/1 = 100.00<br><small>23:30 intervalo 40 min -> metade 1200 s limitada a 900 s; -900 s -> cumprida</small> | 1/1 = 100.00 | sim |
| 11 | 15 min | 0 | 25/11 00:00 – 25/11 00:15 | 2026-11-24 | 25/11 00:10: +120 s / 80% | 1/1 = 100.00<br><small>24:10 GTFS do dia 24/11 = 00:10 de 25/11; tol 900 s; +120 s -> cumprida</small> | 1/1 = 100.00 | sim |
| 12 | faixa horária | 0 | 24/11 05:00 – 24/11 06:00 | 2026-11-24 | 24/11 05:00: +600 s / 80%<br>24/11 05:20: +0 s / 79%<br>24/11 05:40: -601 s / 100% | 1/3 = 33.33<br><small>05:00 sim, 05:20 não, 05:40 não -> 1/3</small> | 1/3 = 33.33 | sim |
| 13 | faixa horária | 0 | 24/11 06:00 – 24/11 07:00 | 2026-11-24 | 24/11 06:00: +300 s / 90%<br>24/11 06:10: +301 s / 90%<br>24/11 06:20: sem viagem<br>24/11 06:30: -300 s / 85% | 2/4 = 50.00<br><small>06:00 sim, 06:10 não, 06:20 não, 06:30 sim -> 2/4</small> | 2/4 = 50.00 | sim |
| 14 | faixa horária | 0 | 24/11 06:00 – 24/11 09:00 | 2026-11-24 | 24/11 06:00: +300 s / 90%<br>24/11 06:10: +301 s / 90%<br>24/11 06:20: sem viagem<br>24/11 06:30: -300 s / 85%<br>24/11 07:00: +900 s / 100%<br>24/11 08:00: +901 s / 100% | 3/6 = 50.00<br><small>06:00, 06:30, 07:00 sim; 06:10, 06:20, 08:00 não -> 3/6</small> | 3/6 = 50.00 | sim |
| 15 | dia | 0 | 24/11 00:00 – 25/11 00:00 | 2026-11-24 | 24/11 05:00: +600 s / 80%<br>24/11 05:20: +0 s / 79%<br>24/11 05:40: -601 s / 100%<br>… (10 partidas) | 5/10 = 50.00<br><small>partidas 05:00..23:30 do dia 24/11 (24:10 cai em 25/11) -> 5/10</small> | 5/10 = 50.00 | sim |
| 16 | dia | 0 | 25/11 00:00 – 26/11 00:00 | 2026-11-24 2026-11-25 | 25/11 00:10: +120 s / 80%<br>25/11 05:00: +900 s / 100%<br>25/11 05:30: +60 s / 100% | 3/3 = 100.00<br><small>24:10 de 24/11 + OFICIAL 25/11 05:00 (+900 s, tol 900 s) e 05:30; GTFS ignorada -> 3/3</small> | 3/3 = 100.00 | sim |
| 17 | dia | 0 | 26/11 00:00 – 27/11 00:00 | 2026-11-26 | 26/11 06:00: +300 s / 100%<br>26/11 06:10: +301 s / 100%<br>26/11 06:20: -300 s / 100%<br>26/11 06:30: +0 s / 80%<br>26/11 06:40: +0 s / 50%<br>26/11 06:50: sem viagem | 3/6 = 50.00<br><small>frequência 600 s -> tol 300 s: +300 sim, +301 não, -300 sim, cob. 80% sim, cob. 50% não, sem viagem</small> | 3/6 = 50.00 | sim |
| 18 | faixa horária | 0 | 26/11 06:00 – 26/11 06:30 | 2026-11-26 | 26/11 06:00: +300 s / 100%<br>26/11 06:10: +301 s / 100%<br>26/11 06:20: -300 s / 100% | 2/3 = 66.67<br><small>frequência: 06:00 sim, 06:10 não, 06:20 sim -> 2/3</small> | 2/3 = 66.67 | sim |
| 19 | mês | 0 | 01/11 00:00 – 01/12 00:00 | 2026-11-24 2026-11-25 2026-11-26 | 24/11 05:00: +600 s / 80%<br>24/11 05:20: +0 s / 79%<br>24/11 05:40: -601 s / 100%<br>… (19 partidas) | 11/19 = 57.89<br><small>soma das contagens: 24/11 (6/11) + 25/11 (2/2) + 26/11 (3/6) = 11/19</small> | 11/19 = 57.89 | sim |
| 20 | dia | 1 | 24/11 00:00 – 25/11 00:00 | 2026-11-24 | 24/11 06:00: +0 s / 100%<br>24/11 06:30: +0 s / 100% | 2/2 = 100.00<br><small>sentido 1 separado do sentido 0: 06:00 e 06:30 no horário -> 2/2</small> | 2/2 = 100.00 | sim |

Resultado: **20/20 conferem**. A mesma bateria executada sobre o código
anterior à revisão divergia em 6 casos (1, 6, 7, 10, 11 e 16): a tolerância de
horário fixo era inferida só das partidas dentro da janela, e a captura GTFS
podia substituir a OFICIAL. Os dois defeitos foram corrigidos.

Verificações adicionais automatizadas: o mês (caso 19) é igual à soma das
contagens dos dias 15–17 (11/19 = 57,89%), enquanto a média simples dos
percentuais daria 66,67%; recalcular os 20 casos gera as mesmas chaves e valores.

## Janela "dia": civil ou operacional (decisão do PO)

Hoje a janela "dia" é o dia civil de São Paulo pela saída prevista, como as
janelas de 15 min, faixa horária e mês. A coluna "Dia(s) operacional(is) da
grade" mostra que a rastreabilidade da data operacional GTFS é preservada: o
caso 11 (00:00–00:15 de 25/11) usa a grade de 24/11.

O diagrama de componentes ("gera partidas por data operacional. Aplica
calendários e horários acima de 24h") e a US23 ("indicadores do dia [...]
consolidados até 06:00 do dia seguinte") sugerem o dia operacional, mas nenhum
critério o define. A diferença só aparece em dias com partida após a meia-noite:

| Dia | Civil (publicado hoje) | Operacional (manual = sistema, não publicado) |
|---|---|---|
| 24/11, sentido 0 | 5/10 = 50,00% (caso 15) | 6/11 = 54,55% (inclui a partida 24:10) |
| 25/11, sentido 0 | 3/3 = 100,00% (caso 16) | 2/2 = 100,00% |
| 26/11, sentido 0 | 3/6 = 50,00% (caso 17) | 3/6 = 50,00% |

O lado operacional também é verificado automaticamente
(`test_alternativa_dia_operacional_confere_com_calculo_manual`). Adotá-lo como
publicação muda o contrato de `IndicadorOperacional`/`GET /indicadores`: o período
de um dia operacional não é um intervalo `[início, fim)` de saídas previstas.
Por isso não foi implementado sem decisão.

## O que falta para o aceite

1. **Decisão do PO** sobre o intervalo de horários fixos (regra provisória acima)
   e sobre a janela "dia": hoje é o dia civil pela saída prevista; a partida
   24:10 entra em 25/11, não no dia operacional 24/11.
2. **Casos reais**: a Instituição Parceira deve indicar ou aprovar 20 casos com
   programação de referência real (linha, sentido, data) nas janelas de 15 min,
   faixa horária, dia e mês. Isso depende de viagens observadas reais, que só
   existirão quando a reconstrução (US14-T01C) gravar `ViagemObservada`.
3. **Conferência da Instituição**: revisar o cálculo manual de cada caso e
   registrar a aprovação abaixo.

| Item | Responsável | Data | Resultado |
|---|---|---|---|
| Decisão do PO sobre intervalo de horário fixo e janela "dia" | | | pendente |
| Casos reais definidos pela Instituição Parceira | | | pendente |
| Aprovação da Instituição Parceira | | | pendente |
