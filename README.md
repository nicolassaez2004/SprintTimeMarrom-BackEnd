# SprintTimeMarrom-BackEnd

## Como iniciar

Requer Python 3.10+ e MySQL 8.0.16+ (para as restrições `CHECK`).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # configure DATABASE_URL e SPTRANS_TOKEN
flask --app app db upgrade   # aplica as migrações em migrations/versions/
python app.py
```

O servidor inicia em `http://127.0.0.1:5000`. Verifique com `GET /health`, que
responde `{"status": "ok"}`. O Flask não cria `/docs` automaticamente.

## Configuração

Para preparar bases separadas de desenvolvimento/teste e conexões no Workbench,
seguir [US12-T01A](docs/US12-T01A.md). O script de conferência usa a conexão real
da aplicação, não apenas `/health`: `python -m scripts.verificar_bases_mysql`.
Para o banco compartilhado do grupo, consultar [conexão Aiven com TLS](docs/AIVEN.md).

Toda configuração vem de variáveis de ambiente (arquivo `.env`, que fica fora do
Git). Nenhuma senha ou token é escrito no código. Use `.env.example` como modelo.

| Variável | Obrigatória | Padrão | Descrição |
| --- | --- | --- | --- |
| `DATABASE_URL` | sim | — | `mysql+pymysql://usuario:senha@host:3306/banco` |
| `SPTRANS_TOKEN` | para coletar | — | Token da API Olho Vivo |
| `SPTRANS_API_URL` | não | `https://api.olhovivo.sptrans.com.br/v2.1` | URL base da API |
| `GTFS_SOURCE_URL` | não | endpoint de download GTFS da SPTrans | URL do ZIP GTFS; pode ser substituída |
| `GPS_COLLECTION_INTERVAL_SECONDS` | não | `30` | Intervalo do coletor, inteiro de 15 a 60 |
| `FLASK_DEBUG` | não | `false` | Ativa o modo debug do servidor |

A API só abre conexão com o banco quando precisa dele, então `/health` responde
mesmo sem o MySQL estar no ar. A conexão real será conferida na US12-T01B.

## Migrações do banco

O esquema é versionado com Flask-Migrate (Alembic), em `migrations/versions/`:

- `0001`: esquema base, idêntico ao da `main` no merge 813b9dc.
- `0002`: alinhamento ao diagrama de classes. Não é destrutiva: só cria,
  renomeia, amplia tipos e copia valores.
- `0003`: metadados de `ArquivoArmazenado` e `VersaoGTFS`, com unicidade por hash.
- `0004`: `ObservacaoTratada` separada do bruto, com FK obrigatória/única para
  `registros_gps` e FK opcional para a versão GTFS. Evidências e limites em
  [US12-T01B](docs/US12-T01B.md).
- `0005`: `verificacoes_gtfs`, agenda e resultado da verificação diária do GTFS
  ([US02-T04](docs/US02-T04.md)).
- `0006`: `linhas` e `paradas`, com chave (versão GTFS, código).
- `0007`: `trechos`, com chaves estrangeiras compostas para linha e paradas da
  mesma versão GTFS.
- `0008`: capturas da programação oficial e `partidas_programadas`, com
  procedência e histórico ([US04-T01](docs/US04-T01.md)).

A importação/consulta de partidas é explícita: `python -m app.programacao --help`.
Consulte [US04-T01](docs/US04-T01.md) para fontes, normalização, testes e o
encaminhamento de fallback quando a fonte oficial está indisponível. A geração
de partidas GTFS depende da continuação da US04.

**Banco novo:** `flask --app app db upgrade`.

**Banco que já tem as tabelas da `main`** (criadas antes das migrações):

```bash
mysqldump -u USUARIO -p BANCO > backup.sql   # DDL no MySQL não tem rollback
flask --app app db stamp 0001                # marca a base como já aplicada
flask --app app db upgrade                   # aplica a 0002
flask --app app db check                     # confere banco x modelos
```

Ciclos gravados antes da `0002` ficam com `recebidos` e `prevista_em` em `NULL`
(valores desconhecidos). Não use `db.create_all()` nem `downgrade`: os downgrades
recusam rodar, porque apagariam dados.

## Estrutura

Segue o diagrama de componentes e serviços:

- `app/__init__.py`: `create_app()`, `/health`, `/api/health` e registro dos Blueprints
- `app/api/`: rotas HTTP. Parâmetros inválidos recebem `400` com `{"erro": "..."}`
  (veja `GET /api/exemplo?linha=8000`)
- `app/integrations/sptrans.py`: `SPTransClient`, comunicação com a API Olho Vivo
  (autenticação, timeout, leitura)
- `app/coleta/bus_collector.py`: `BusCollector` (um ciclo de coleta) e o processo
  coletor periódico, separado do Flask
- `app/coleta/qualidade.py`: validação dos campos GPS, deduplicação pela chave
  completa e mensuração de cobertura para um período explicitamente observado
- `app/gtfs/`: validação, captura e versionamento imutável do ZIP GTFS por SHA-256
- `app/integrations/gtfs.py`: cliente HTTP para o feed configurado
- `app/models.py`: modelos SQLAlchemy

## Modelo de dados

- `veiculos` (`Veiculo`): o ônibus, identificado pelo prefixo.
- `onibus_selecionados` (`OnibusSelecionado`): configuração que indica quais
  veículos a coleta monitora. Remover uma seleção não afeta o histórico.
- `registros_gps` (`RegistroBrutoGPS`): uma posição bruta. `payload` guarda o
  objeto do veículo exatamente como a API devolveu; as colunas tipadas
  (`prefixo_origem`, `linha_origem`, `latitude`, `longitude`, `horario_gps`) são
  uma leitura dele, com `NULL` para valores ausentes ou inválidos. Validar e
  classificar a qualidade fica para o tratamento (`ObservacaoTratada`).
- `execucoes_coleta` (`ExecucaoColeta`): uma linha por ciclo, com `fonte`,
  `prevista_em`, início, fim, `estado` (`EM_ANDAMENTO`, `OK`, `PARCIAL` ou
  `FALHA`), `tentativas`, `recebidos`, `novos_persistidos` e `erro`. É
  diferente de `LacunaColeta`, o intervalo sem cobertura.

**Duplicação.** Cada registro aponta ao ciclo que o gravou primeiro.

- A chave completa de posição é `(prefixo_origem, horario_gps)`. Repetições
  dessa chave no lote ou no banco não são gravadas novamente; a constraint
  `uq_registros_gps_prefixo_horario` também protege gravações concorrentes.
- Se qualquer parte da chave estiver ausente, o registro é preservado, inclusive
  quando outro registro também tem campos nulos. `NULL` não é convertido em
  valor sentinela nem usado para inferir que dois registros são iguais.
- Coordenadas e horário inválidos/ausentes ficam `NULL`; o payload original é
  mantido. Coordenadas presentes fora da faixa e horários sem fuso são inválidos.

**Confirmação do lote.** O ciclo começa como `EM_ANDAMENTO`; depois da coleta,
os registros brutos, os veículos novos e o resumo final do ciclo são confirmados
juntos em uma transação. `OK` e a contagem de novos registros só ficam gravados
com o `commit`. Se a gravação falhar, o lote inteiro sofre `rollback` e o ciclo
é registrado como `FALHA`. A consulta prévia de duplicatas reduz trabalho, mas a
constraint `uq_registros_gps_prefixo_horario` é quem resolve colisões concorrentes.

Para contar ciclos em uma janela de tempo:

- ciclos esperados = duração da janela ÷ `GPS_COLLECTION_INTERVAL_SECONDS`
- ciclos gravados = linhas de `execucoes_coleta` com `iniciada_em` na janela

`app.coleta.qualidade.medir_cobertura(inicio, fim, intervalo_segundos)` retorna
ciclos esperados, observados e percentual para a janela `[início, fim)`. Início e
fim com fuso são obrigatórios; portanto, não há aceite de meta sem período
observado. A meta mensal padrão é 99% e `relatorio.meta_atingida` informa se foi
alcançada. O período e o intervalo usados acompanham a medição para torná-la
auditável.

**Política de horário:** todos os instantes são gravados em UTC, em
`DATETIME(6)` no MySQL. Os modelos convertem horários com fuso para UTC, recusam
horários sem fuso e devolvem sempre `datetime` com `tzinfo=UTC`.
`registrado_em` é o início do ciclo de coleta; `horario_gps` é o instante
informado pela SPTrans (`ta`).

## Coleta de GPS

O coletor consulta cada linha selecionada uma vez por ciclo e grava as posições
dos veículos selecionados. O primeiro ciclo começa imediatamente; os próximos
seguem o intervalo configurado (30 s por padrão). Ele roda em um processo
separado da API:

```bash
python -m app.coleta.bus_collector
```

- Importar ou reiniciar a API (inclusive pelo reloader ou com vários workers
  WSGI) não inicia o coletor.
- Um lock nomeado no banco (`GET_LOCK` no MySQL) impede dois coletores no
  mesmo banco: o segundo encerra com código 1. O lock é conferido a cada ciclo;
  se ele se perder, o coletor para.
- Cada ciclo usa uma sessão SQL nova e é registrado em `execucoes_coleta`,
  inclusive quando falha.

Para consultar as posições atuais sem gravá-las, use `--localizar` com um
prefixo ou `--linha` com o letreiro da linha:

```bash
python -m app.coleta.bus_collector --localizar 5810
python -m app.coleta.bus_collector --linha 5810
```

## Cliente SPTrans

`SPTransClient` (`app/integrations/sptrans.py`) autentica com
`POST /Login/Autenticar` e reutiliza a sessão (cookie) nas consultas. Se a
sessão expirar (HTTP 401), autentica de novo uma única vez. Métodos:
`buscar_todas_posicoes()` (`GET /Posicao`, toda a frota), `buscar_posicoes(cl)`
e `buscar_linhas(termo)`.

As respostas são devolvidas como a API enviou, com `hr` e `ta` intactos. O
cliente só confere o formato. `{"hr": ..., "l": []}` é uma consulta válida sem
veículos, e não um erro. As falhas derivam de `SPTransError`:

| Exceção | Quando |
| --- | --- |
| `SPTransAutenticacaoError` | o token foi recusado (`/Login/Autenticar` ≠ `true`) |
| `SPTransHTTPError` | status HTTP ≥ 400 (`status_code` disponível) |
| `SPTransConexaoError` | timeout ou falha de rede |
| `SPTransRespostaInvalidaError` | a resposta não é JSON ou está sem `hr`/lista |

As mensagens nunca incluem o token, cookies ou a URL de autenticação.

## Captura do GTFS

O código já usa o endpoint de download GTFS da SPTrans por padrão. Se a fonte
mudar, substitua `GTFS_SOURCE_URL` no `.env`. Aplique as migrações e execute uma
captura e a importação da rede com:

```bash
python -m app.gtfs
```

Depois da captura, o comando importa do ZIP guardado `routes.txt` e `stops.txt`
(`Linha`, `Parada`) e depois os `Trecho`s, a partir de `trips.txt`,
`stop_times.txt` e `shapes.txt` (este último é opcional). Cada registro guarda a
versão GTFS de origem, então o mesmo `route_id`/`stop_id` em outra versão é
outro registro. A importação de uma versão acontece numa única transação e,
se repetida, não duplica registros.

Um trecho liga duas paradas consecutivas. Viagens com a mesma linha, sentido
(`direction_id`) e sequência de paradas formam um **trajeto**, identificado
pelo menor `trip_id` delas (`trechos.trajeto`). Assim, variantes da mesma linha
não se misturam, e cada uma tem `ordem` a partir de 1. `sentido` é o
`direction_id` (obrigatório). A geometria é uma coluna espacial `LINESTRING`
(x = longitude, y = latitude) recortada do `shape`; sem `shape`, é a reta entre
as paradas. Coordenadas de paradas são `DECIMAL(11,8)`. O ZIP é lido pelo
`FileStore`, conferido contra o hash da versão.

O sistema baixa para arquivo temporário, confere a integridade do ZIP e os
arquivos básicos GTFS, calcula SHA-256 e só então grava o original em
`instance/gtfs/<hash>.zip`. O arquivo nunca é sobrescrito. Metadados de
`ArquivoArmazenado` e `VersaoGTFS` são confirmados na mesma transação; hash
repetido retorna a versão existente. `feed_info.txt`, quando presente, informa a
vigência; sem ele, o início usa a data da captura e o fim fica `NULL`. Falha de
download/validação não cria versão nem altera versões anteriores.

O `FileStore` em `app/storage.py` publica/verifica o ZIP antes de reutilizar uma
versão pelo hash. Se o original estiver ausente, um novo download do mesmo
conteúdo o recupera sem duplicar metadados nem alterar a data da primeira captura.
Se o arquivo existente tiver conteúdo divergente, a operação falha sem
sobrescrevê-lo. O hash é dos bytes do ZIP, não dos CSVs descompactados.

Configure `GTFS_STORAGE_DIR` para mudar o diretório físico. Compartilhar somente
o MySQL da Aiven **não compartilha os ZIPs**: os processos que consomem o arquivo
devem acessar o mesmo volume/armazenamento do coletor. Para desenvolvimento em
computadores distintos, cada captura garante a cópia no seu próprio diretório,
mas versões antigas que nunca foram baixadas naquela máquina continuam apenas
no armazenamento de origem. A chave `gtfs/<hash>.zip` é relativa ao FileStore.

Um rollback SQL preserva o objeto final imutável, pois outra captura pode estar
prestes a confirmá-lo. A repetição reaproveita esse objeto; somente `.part`
privados são removidos imediatamente. Assim, falhas podem deixar ZIPs sem
metadados: considerar esses arquivos no uso de disco e no backup. Não remover
esses objetos enquanto houver capturas em andamento; eventual limpeza de
órfãos precisa de coordenação e não faz parte desta tarefa.

## Verificação diária do GTFS US02-T04

O agendamento roda em um processo separado da API e do coletor GPS. Após o
responsável pelo banco revisar e aplicar a migração **0005**, execute na raiz
do back-end, usando o ambiente virtual do projeto:

```bash
python -m app.tarefas
```

No Windows, o equivalente é `.\.venv\Scripts\python.exe -m app.tarefas`.
O processo precisa permanecer ativo; `Ctrl+C` o encerra. Não há instalação
automática de serviço ou tarefa do Windows. Iniciar o Flask não inicia essa
verificação. Para uma tentativa manual com o mesmo controle de exclusividade:

```bash
python -m app.tarefas --uma-vez
```

- Sem histórico, a primeira verificação é imediata. Após sucesso, a próxima
  segue a cadência de 24 horas em UTC, sem somar o tempo de processamento.
  Reiniciar consulta a agenda persistida, sem repetir uma verificação ainda
  dentro do prazo. Horários perdidos não geram uma rajada de capturas.
- Falhas geram nova tentativa após 15 minutos. Uma tentativa interrompida é
  marcada como falha e retomada. Um lock MySQL distinto por banco impede
  dois processos de tarefas simultâneos sem bloquear o coletor GPS.
- `verificacoes_gtfs` guarda agenda, início/fim, resultado, hash identificado,
  primeira identificação, confirmação de persistência, versão e tipo de erro.
  O estado é `OK`, `PARCIAL`, `FALHA` ou `EM_ANDAMENTO`; o resultado distingue
  `NOVA`, `REPETIDA` e `FALHA`. O histórico das versões anteriores é preservado.
- A identificação acontece depois de baixar e validar o ZIP e calcular seu
  hash. Ela é gravada antes da publicação/inserção. Falhar e tentar novamente
  o mesmo hash não reinicia o prazo. A confirmação é medida depois do commit
  da nova versão. Repetições não recebem uma nova data de entrada na base.
- Atrasos de agendamento a partir de 60 segundos geram aviso; entrada de uma
  nova versão após mais de 24 horas gera alerta e estado `PARCIAL`, mesmo que
  a versão tenha sido preservada. Falhas de captura ficam no histórico.
- Os eventos também são gravados como JSONL em
  `instance/logs/gtfs-tarefas.jsonl`, com rotação de 10 MB e cinco cópias.
  `GTFS_TASK_LOG_FILE` permite mudar esse caminho. Se o banco estiver
  indisponível, o log registra o tipo de falha e o processo tenta novamente;
  ele não cria tabelas nem executa migrações para contornar o erro.

**Limite da medição:** o instante de publicação na SPTrans não é conhecido.
O prazo medido começa na primeira identificação local persistida do hash
válido. Os testes simulam dias e falhas; não representam uma observação real
de 24 horas nem garantem disponibilidade quando o processo estiver desligado.

**Aceite pendente:** a tarefa adota verificação diária conforme sua descrição.
O PO ainda precisa confirmar a divergência diária/semanal do documento, e
outra pessoa deve revisar a alteração antes de movê-la para Pronto. Veja a
[evidência da US02-T04](docs/US02-T04.md).

**Consulta real (manual e autorizada).** Coloque o token em `SPTRANS_TOKEN` no
`.env`, nunca na linha de comando nem no código, e rode:

```bash
SPTRANS_TESTE_REAL=1 python -m pytest tests/test_sptrans_client.py -k real -s
```

O comando mostra só `hr` e o total de linhas e de veículos. Sem
`SPTRANS_TESTE_REAL=1`, esse teste é ignorado.

## Testes

```bash
python -m pytest -v
```

Os testes usam SQLite em memória e não precisam de MySQL nem de `.env`. Nenhum
teste automatizado chama a SPTrans de verdade.
