# SprintTimeMarrom-BackEnd

## Modelagem da seleção de ônibus e do registro de GPS

`OnibusSelecionado`, em `app/models.py`, representa os ônibus SPTrans que a
aplicação mantém selecionados no banco, identificados pelo prefixo do veículo
(campo `p` da resposta da API) e pelo código da linha usado para consultar a
API. Cada amostra em `RegistroGPS` referencia um
prefixo existente nessa tabela por chave estrangeira. Assim, só há registro de
posição para ônibus presentes na seleção, e a exclusão de um ônibus com
histórico é restringida pelo banco.

Cada linha de `registros_gps` representa uma observação e contém:

- `prefixo_onibus`: chave estrangeira para `onibus_selecionados.prefixo`;
- `codigo_linha`: código da linha reportado pela SPTrans (`cl`), que pode
  mudar entre observações e por isso fica associado à amostra;
- `latitude` e `longitude`: coordenadas em graus decimais, obrigatórias e
  limitadas aos intervalos geográficos válidos;
- `registrado_em`: instante da captura em UTC, com valor padrão no momento da
  criação;
- `id`: identificador único da amostra. Amostras do mesmo ônibus e horário não
  são impedidas pelo modelo.

O índice composto por `prefixo_onibus` e `registrado_em` facilita consultar o
histórico de cada veículo em ordem temporal.

## Coleta periódica

O módulo `app/services/gps_collector.py` implementa o ciclo de checagem. Para
iniciá-lo em um processo separado da API, execute:

```bash
python -m app.services.gps_collector
```

O coletor consulta o banco em busca de ônibus selecionados, agrupa-os pelo
código de linha, consulta cada linha uma vez na SPTrans e grava posições apenas
para prefixos selecionados. A API identifica os veículos pelo campo `p` e
retorna coordenadas nos campos `py` (latitude) e `px` (longitude). Cada ciclo
usa um mesmo instante UTC em `registrado_em` para as posições obtidas naquele
ciclo.

O primeiro ciclo inicia imediatamente; os seguintes respeitam o intervalo
configurado. Se uma consulta ultrapassar o prazo, o coletor não sobrepõe ciclos
nem faz catch-up: aguarda um intervalo completo após o ciclo atrasado. Linhas
com erro são registradas no log e as demais continuam sendo processadas. Para
encerrar o processo, envie Ctrl+C.

Configure `GPS_COLLECTION_INTERVAL_SECONDS` no `.env`; o padrão é 30 segundos e
os únicos valores aceitos vão de 15 a 60, inclusive:

```dotenv
GPS_COLLECTION_INTERVAL_SECONDS=30
```

O servidor Flask não importa nem inicia o agendador. Portanto, o reloader e os
workers WSGI não duplicam o coletor, e reiniciar a API não cria outro processo
coletor. Um PostgreSQL advisory lock também impede dois processos coletores
simultâneos no mesmo banco. Cada ciclo remove sua sessão SQL ao terminar.

Cada ciclo é registrado em `execucoes_coleta`, com início, fim, status,
quantidade de posições e eventual erro. As tabelas novas precisam ser aplicadas
ao banco por migração antes de iniciar o processo.

Para imprimir a localização atual de um veículo pelo prefixo, sem gravar no
banco, execute na raiz do projeto:

```bash
python -m app.services.gps_collector --localizar 5810
```

Se `5810` for o número/letreiro da linha, consulte os veículos daquela linha:

```bash
python -m app.services.gps_collector --linha 5810
```

Ambos os diagnósticos exigem `SPTRANS_TOKEN` no `.env`. Se aparecer
`ModuleNotFoundError: No module named 'app'`, execute o comando a partir da raiz
do repositório, onde está o diretório `app`.

O repositório não continha uma tabela/modelo de seleção de ônibus, então
`OnibusSelecionado` formaliza esse cadastro. A seleção deve gravar o prefixo e
o código da linha antes de iniciar a coleta. As tabelas devem ser
criadas/aplicadas ao banco por uma migração de esquema (ou, em ambiente local
descartável, pela criação do metadata do SQLAlchemy).

## Testes

Execute os testes com:

```bash
python -m unittest discover -s tests -v
```

O relógio controlado verifica cadências de 15, 30 e 60 segundos sem chamadas à
API real; outro teste confirma que criar a aplicação Flask não inicia o coletor.
