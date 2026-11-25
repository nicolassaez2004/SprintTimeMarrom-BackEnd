# Review integrada — US10, US17, US18 e US23-T03 (Sprint 2)

Data: 25/11/2026. Base: `main` em `3f1ab82` (backend) e `a50a158` (frontend).
Branch `review/us10-us17-us18-us23` nos dois repositórios; **sem commit, push ou merge**.
Bancos: somente SQLite em memória/arquivo descartável. Nada foi executado na Aiven.

## Defeitos encontrados e corrigidos

| P | Defeito | Correção |
| --- | --- | --- |
| P0 | A agenda US23-T03 recalculava só US14/US16, mas `/series` marcava regularidade e espera como `consolidado: true` apenas pelo horário da janela. O CSV diário exportava esses indicadores sem que a consolidação os tivesse produzido. | `agenda.py` passa a recalcular frequência (US15), regularidade (US17) e espera/excesso (US18) nas atualizações de 15 min e na consolidação, gravando só as janelas da publicação. `consolidado` passa a exigir uma consolidação que recalculou aquele indicador (`parametros.indicadores`). Agregações de vários dias exigem todos os dias. |
| P0 | Publicação podia misturar conjuntos: uma nova captura da programação muda a chave, e o registro antigo da mesma janela ficava ao lado do novo (API, `/series` e CSV). | `_substituir`: na mesma transação, remove registros do mesmo escopo (linha, sentido, GTFS, correspondência, indicador, regra) dentro do período que o novo conjunto não reproduziu. Registrado em `parametros.substituidos`. |
| P1 | US17: CV exatamente 0,5 calculado com intervalos fracionários dava `0.5000000000000001` e era sinalizado como agrupamento, enquanto o valor publicado era `0.5`. | Agrupamento decidido sobre o valor publicado (6 casas). |
| P1 | US10: licença do CSV divergia do requisito ("mesma licença da US09" = CC BY 4.0) e do Parquet da US09. O arquivo baixado não indicava fonte e licença. | `LICENCA = "CC BY 4.0"`; colunas `fonte` e `licenca` em cada linha; condições da SPTrans mantidas em `condicoes_fonte` nos metadados. |
| P1 | US10: `tipo=ESPERA`, `FREQUENCIA` ou `REGULARIDADE_INTERVALOS` com `data=` respondia 400 no download (regras de agregação de `/indicadores`). `ESPERA` e `FREQUENCIA` também não correspondiam aos tipos persistidos. | O tipo é validado à parte; os tipos compostos da API são expandidos (`ESPERA` → espera + excesso). |
| P1 | US17/US18-T03B: os relatórios declaravam `pronto: true` com aceite "dispensado pelo responsável", embora o requisito exija os 20 casos aprovados pela Instituição Parceira. | `pronto` só com `aceite_institucional = APROVADO`; a conferência técnica fica em `conferencia_tecnica_ok`. Cadernos marcados como `PENDENTE`. |
| P1 | US18-T03B não cobria horários após a meia-noite. | O C12, que repetia a insuficiência do C05, foi trocado por operação após 24h: conta manual 6,875 / 1,875 min, confere. |
| P1 | Frontend: CV 0,5004 (agrupamento) e 0,4996 (sem agrupamento) apareciam ambos como "0,5". Um excesso de −0,004 min aparecia como "0 min". | O CV é exibido com as 6 casas publicadas. Um valor diferente de zero nunca é exibido como 0 nem perde o sinal. |
| — | `/indicadores` não informava provisório × consolidado. | Mesmo bloco `atualizacao` e campos `consolidado`/`consolidado_em` de `/series` (aditivo). OpenAPI atualizado. |

## Evidências (automatizadas, dados sintéticos)

- Backend: `pytest` completo **899 passed, 10 skipped** (antes: 881/10). Os skips são MySQL
  real (sem URL descartável), PyYAML ausente no `.venv` e o teste manual SPTrans. O teste do
  OpenAPI foi rodado à parte com PyYAML temporário: 3 passed.
- Frontend: `node --test tests/` **103/103** (antes 101) e `tests/test_servidor.py` 3/3.
- Regressões novas: `tests/indicadores/test_agenda_review_us17_us18.py`, com 13 casos. Relógio
  controlado em 00:00, 03:59, 04:00, 05:59, 06:00 e 06:01 de São Paulo; substituição de
  conjunto; janelas após 24h; falha durante a publicação seguida de retomada; CSV × API.
  Os 12 primeiros foram rodados também contra o código original: **6 falham** (defeitos
  reais). Os 6 de relógio passam, confirmando o comportamento de horário que já existia.
- 20 casos: US17 20/20 e US18 20/20 `CONFERIU_EXEMPLO` (`docs/evidencias/US1{7,8}-T03B-conferencia.json`).
  Os relatórios foram regenerados sobre a árvore de trabalho desta review; o `codigo_base`
  aponta `3f1ab82`, ainda sem as alterações não commitadas.
- Ensaio ponta a ponta em SQLite descartável: a agenda consolidou o caso C01 com 1440
  registros e publicou o CSV. API, CSV e painel (Firefox headless) mostram os mesmos valores:
  CV 0,28782 sem agrupamento, espera 2,346154 min, excesso −0,153846 min e NULL como vazio
  ou "Sem resultado".

**Não comprovado:** publicação real até 06:00, que exige produção. Também não houve
integração em MySQL 8: sem acesso ao Docker e só MariaDB local. Nenhum aceite institucional.

## Pendências

**P0 — dependência externa:** nenhum processo de produção cria `ViagemObservada`
(reconstrução US14) nem `PassagemEstimada` (US15-T01); só scripts de validação o fazem. Sem
isso, a agenda não encontra contextos e não publica nada: os fluxos estão corretos, mas
vazios em produção. Fora do escopo desta review.

**P1 — decisões do PO / Instituição Parceira**
1. Enviar e aprovar os 20 casos da US17-T03B e da US18-T03B. Nada foi enviado.
2. US18-T02: regra `E_prog = ΣH² ÷ (2·ΣH)` para janelas que cruzam faixas de programação.
   Foi escolhida pela equipe e está sem aprovação.
3. Licença: confirmar a compatibilidade do CC BY 4.0 com as condições da SPTrans ("não
   sublicenciar"). Isso vale para o CSV (US10) e para o Parquet (US09).

**P2**
- Custo: cada atualização de 15 min recalcula US15/17/18 pela data inteira de cada contexto
  (US27/US31).
- Dias perdidos com o processo parado: só o dia anterior é consolidado.
- CSV com vírgula e ponto decimal: o Excel em pt-BR não separa as colunas.
- O agrupamento só aparece dentro de `qualidade` (JSON).
- O painel mostra só janelas de 15 min. As agregações de faixa, dia e mês da US17/US18
  existem só na API.
- No gráfico, o eixo cobre o dia inteiro mesmo com faixa diária selecionada (US23-T02B).
