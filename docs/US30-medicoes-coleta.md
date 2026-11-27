# US30 — medições de sucesso da coleta

`app.monitoramento.sucesso_coleta.serie_sucesso_coleta(inicio, fim, janela=...)`
consulta ciclos Olho Vivo pela data prevista, ou início quando ausente, e
retorna pontos UTC semiabertos com IDs e contagens por categoria. A janela
padrão é uma hora; a consulta aceita até 31 dias. O serviço é somente leitura.

A taxa é `sucesso / (sucesso + falha própria)`. Falhas da fonte são excluídas;
parciais, em andamento e causas desconhecidas aparecem separadamente e não
entram na taxa. Denominador zero produz `null`. A série conta ciclos registrados,
sem estimar ciclos ausentes nem medir a confirmação em 60 segundos da US29.

A classificação reconhece as causas registradas pela US29: rede, timeout,
resposta inválida e HTTP 408, 425, 429 ou 5xx são falhas da fonte. Autenticação,
configuração, outros HTTP e erros internos conhecidos são falhas próprias.
Causas ausentes ou não reconhecidas ficam desconhecidas, pois não provam a
origem do erro. A classificação é independente da métrica existente da US29.

Verificação local: `python -m pytest -q tests/monitoramento/test_sucesso_coleta_us30.py tests/resiliencia/test_us29.py tests/qualidade/test_qualidade_coleta.py`.
Os testes usam ciclos sintéticos e conferem IDs, categorias e denominador.
O serviço ainda não está ligado a uma rota ou painel publicado; esses itens são
dependências externas desta tarefa e precisam de verificação em ambiente real.
A entrega deve ser revisada por outra pessoa antes de ser marcada como pronta.
