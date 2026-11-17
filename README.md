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

O primeiro ciclo inicia imediatamente; os seguintes começam a cada 30 segundos.
Se uma consulta demorar mais que esse intervalo, o coletor não sobrepõe ciclos:
aguarda 30 segundos após o ciclo atrasado antes de iniciar o próximo. Linhas
com erro são registradas no log e as demais continuam sendo processadas. Para
encerrar o processo, envie Ctrl+C.

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
