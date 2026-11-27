# US30 — disponibilidade mensal e lacunas

`app.monitoramento.disponibilidade_mensal.disponibilidade_mensal('AAAA-MM')`
consulta as sondagens de API e painel e retorna contagens e taxas independentes.
O mês é civil em America/Sao_Paulo. Apenas minutos inteiros já transcorridos
entram na análise; mês futuro é rejeitado e mês em andamento é marcado parcial.

Para cada alvo, espera-se uma sondagem por minuto. Minutos sem registro são
lacunas, nunca sucessos ou quedas presumidas. Se houver mais de um registro no
mesmo minuto, conta um único minuto e uma única falha basta para classificá-lo
como falha. Contam-se também os registros adicionais para auditoria. A taxa
observada é `minutos de sucesso / minutos observados`, com denominador zero
resultando em `null`. A cobertura é `minutos observados / minutos esperados`.

A taxa observada é comparada a 99% sem arredondamento prévio. A meta mensal só
é afirmada ou rejeitada quando o mês terminou e não há lacunas; caso contrário
`meta_mensal_comprovada` é `null`. Mês parcial ou dados ausentes não comprovam
a meta, mesmo que a taxa observada seja de 100%. Os limites exatos da consulta,
contagens e registros excedentes são retornados para conferência no banco.

A falha da sondagem informa que o acesso falhou, não sua causa. O relatório
apresenta separadamente as categorias dos ciclos de coleta US29, incluindo
falha da fonte e falha própria, sem presumir que uma originou a outra. Ciclos
não registrados não são inventados. O monitor não grava medições se o banco
estiver indisponível: isso aparece como lacuna, não como sucesso.

Testes: `python -m pytest -q tests/monitoramento/test_disponibilidade_mensal_us30.py tests/monitoramento/test_sucesso_coleta_us30.py`.
Dados de teste são sintéticos. A consulta não cria rota nem publica painel; a
implantação da sondagem e a revisão por outra pessoa continuam necessárias.
