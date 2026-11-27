# US30 — GET /saude

`GET /saude` e `GET /api/saude` retornam o mês corrente em
America/Sao_Paulo. O parâmetro opcional `mes=AAAA-MM` consulta um mês passado;
meses futuros, parâmetros desconhecidos ou repetidos retornam 400.

A resposta inclui o período UTC, `mes_parcial`, taxas e denominadores por
serviço, última medição de cada alvo no período analisado, categorias dos ciclos
de coleta e até 100 lacunas de coleta, com total e indicador de truncamento.
A última medição não representa necessariamente a situação atual. As lacunas
mostram seu estado no corte do relatório; dados internos da causa não são
expostos na rota pública. A política e as limitações acompanham a resposta.
A meta mensal é inconclusiva (`null`) para mês parcial ou cobertura incompleta.

A rota utiliza o limitador público por IP existente. Uma falha de consulta do
banco retorna 503 sem expor detalhes internos. As taxas dependem da migração
US30, da execução externa da sondagem e dos ciclos persistidos: sem dados, não
há comprovação de disponibilidade. O frontend e o ambiente de produção ainda
precisam ser integrados e verificados. A revisão por outra pessoa permanece
necessária antes de mover a tarefa para Pronto.

Verificação: `python -m pytest -q tests/monitoramento/test_saude_us30.py tests/monitoramento/test_disponibilidade_mensal_us30.py tests/monitoramento/test_sucesso_coleta_us30.py`.
