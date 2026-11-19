# Review integrada — US22-T01, US13-T01/T02, US05-T01/T02, US09-T02

Data: 19/11/2026. Branch `review/us22-us13-us05-us09`, a partir da `main` em `c190a49`
(PRs #22–#25). Sem commit.

## Problemas encontrados e corrigidos

| Tarefa | Problema | Correção |
|---|---|---|
| US22-T01 | `/posicoes/atuais` agrupava (`GROUP BY`) todos os brutos das 24 h: 3,9 s com 1 h de frota (1,68 mi registros) em MariaDB; dezenas de segundos com 24 h. Inviável para mapa ao vivo. | Busca por veículo no índice `(prefixo_origem, coletado_em)`, sem filesort: 0,45 s (1,68 mi) e 0,48 s (3,36 mi). Mesmo resultado; empate no ciclo fica com o maior `id`. Teste novo. |
| US13-T02 | Critério "resposta documentada" sem documentação. | `docs/US13.md` com parâmetros, resposta, situações e regras. |
| US09-T02 | Ciclo do dia D gravado depois da exportação (checkpoint da US29 sincronizado quando o MySQL volta) deixava um Parquet marcado como completo sem esses registros, para sempre. | Espera ciclo do dia em andamento (até 06:00) e regera arquivos dos últimos 7 dias quando um ciclo do dia é finalizado após `generated_at_utc`. 3 testes novos. |
| US09-T02 | `?data=9999-12-31` gerava erro 500 (`OverflowError` no cálculo do prazo). | 404 `ARQUIVO_AINDA_INDISPONIVEL` com `previsto_ate: null`. Teste novo. |
| US22, US05, US09-T02 | Contratos públicos sem documentação. | `docs/US22-T01.md`, `docs/US05.md`, `docs/US09-T02.md`; pendências de `docs/US09-T01.md` atualizadas. |

## Evidências

- Suíte: 452 aprovados, 6 ignorados (opt-in MySQL/SPTrans). Seis tarefas: 113 aprovados.
- MariaDB 13 local descartável (não MySQL 8, não Aiven): cadeia completa `0001 → us05_0014`
  em base vazia; `flask db check` sem diferenças; ensaio integrado 18/18 (filtros e PDF da
  US05, FK RESTRICT e CHECK aplicados pelo banco, posições e filtro da US22, contadores,
  lacunas registrada e inferida aberta da US13, publicação, catálogo, download e regeração
  com dado atrasado da US09).
- Medidas: US22 0,45–0,48 s (14 mil veículos); US13 0,94 s (1 dia) e 1,29 s (31 dias com
  janelas) com 1 ano de ciclos; exportação Parquet 95 mil registros/s.

## Riscos registrados (fora do escopo destas tarefas)

- US13: filtro por `COALESCE(prevista_em, iniciada_em)` não usa índice; o custo cresce
  linearmente com o histórico (US27/US31; exige coluna indexada e migração).
- US13: "ciclos esperados" usa o intervalo configurado hoje para todo o histórico.
- US09: prazo das 06h depende de `python -m app.tarefas` ativo; sem serviço instalado.
