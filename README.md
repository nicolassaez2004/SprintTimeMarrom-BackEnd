# SprintTimeMarrom-BackEnd

## Base mensal para associações — US21-T01

[Preparação rastreável](docs/US21-T01.md) combina desempenho, chuva, proxy de
tráfego e infraestrutura em uma observação por trecho/rede/mês. Mantém versões,
coberturas, fontes, exclusões e hash, sem junções que multipliquem a amostra.
Reproduzir o controle com `python -m scripts.validar_us21_t01` (SQLite em memória).
Não calcula associações; política e análise dependem de aprovação.

### Plano de análise e incerteza — US21-T02

[Proposta para analista e PO](docs/US21-T02.md) define desfecho, controles e
tratamento da dependência temporal/espacial. Situação: aprovação pendente,
sem coeficientes ou intervalos calculados. Conferir prontidão com
`python -m scripts.verificar_plano_us21`.

## Conferência de propagação de atrasos — US19-T04

[Vinte casos controlados e resultados](docs/US19-T04.md), com diferenças de
atraso, encadeamento do veículo, correlação em quatro escalas e ranking mensal.
Reproduzir com `python -m scripts.validar_us19_t04`; somente SQLite em memória.
Aceite institucional e revisão pendentes. Os agregados intradiários da
variação por trecho ainda não são implementados pela US19 atual.

## Conferência de passagem e frequência — US15-T04

[Caderno de 20 exemplos controlados](docs/US15-T04.md), com cálculo manual nas
escalas de 15 minutos, faixa horária, dia e mês. Reproduzir com
`python -m scripts.validar_us15_t04`; o ensaio usa somente SQLite descartável.
Revisada em 24/11/2026; aceite da Instituição Parceira dispensado pelo responsável.

## Chuva e temperatura por trecho — US06

[Estações e observações de clima](docs/US06.md) (INMET horário; CGE só com
acesso), vínculo de cada trecho ao posto mais próximo e
`GET /contexto/clima`. Ausência de medição é `null`, nunca chuva zero. A
coleta roda em `python -m app.tarefas`; a cobertura mensal, em
`python -m app.contexto.clima cobertura --mes AAAA-MM`. Migração `us06_0020`.

## Painel histórico — US23-T01

[`GET /series`](docs/US23-T01.md) aceita linha, sentido, período e faixa horária
diária. Reutiliza indicadores persistidos, declara limites do histórico e
preserva zero, resultados nulos e intervalos sem publicação.

## Diferença entre saídas observadas e programadas — US16-T01

O serviço de [pontualidade por viagem](docs/US16-T01.md) calcula a diferença
assinada para oferta fixa com saída confirmada e pareamento explícito. Separa
frequência e viagens sem evidência suficiente, preservando fonte e versão.
O denominador proposto aguarda confirmação do PO.

## API pública — US11

`GET /indicadores` e `GET /series`: [contrato da T01](docs/US11-T01.md) e
[limite compartilhado de 60 consultas/minuto por IP](docs/US11-T02.md).
O limitador exige a migração `us11_0016`; o banco e a lista de proxies
confiáveis devem seguir as instruções de operação do documento.

Guia público em **`/docs/indicadores`**, com OpenAPI, exemplos curl/JavaScript/
Python, tratamento de 429 e evidência HTTP. No [ensaio local da US11-T03](docs/US11-T03.md),
100 consultas sobre 100 mil valores sintéticos tiveram p95 de **61,41 ms**;
o documento registra o ambiente e os limites dessa medição, sem afirmar SLA da Aiven.

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

Com o ambiente e o `.env` configurados, abra `app.py` no VS Code e clique em
**Executar Arquivo Python**. A API e a coleta de ônibus começam juntas usando
o MySQL definido no `.env`. Depois, execute `servidor.py` no frontend.
O mapa consulta o último ciclo com posições gravadas por meio de
`GET /posicoes/atuais?modo=ultima_coleta`. Como cada bruto pertence ao ciclo que
o gravou primeiro, o modo lê também os 60 s anteriores a esse ciclo: ônibus com
GPS inalterado continuam no mapa, marcados como `recente: false` se passarem de 60 s.

Execução separada do coletor: [docs/COLETOR.md](docs/COLETOR.md). Migrações
pendentes na Aiven: [docs/AIVEN-MIGRACOES-MVP.md](docs/AIVEN-MIGRACOES-MVP.md).

O servidor inicia em `http://127.0.0.1:5000`. Verifique com `GET /health`, que
responde `{"status": "ok"}`. O Flask não cria `/docs` automaticamente.

## Demonstração e validação (Sprints 1 e 2)

Banco SQLite descartável com dados **sintéticos** que passam pelo pipeline real
(passagens → agenda de 15 min → consolidação → CSV/Parquet), sem Aiven nem SPTrans:

```bash
python -m scripts.demo_sprints_1_2 --pasta /tmp/demo-marrom          # prepara (pasta vazia)
python -m scripts.demo_sprints_1_2 --pasta /tmp/demo-marrom --servir # API em :5000
```

Roteiro de conferência, resultados esperados e parecer por história:
[docs/reviews/sprints-1-2.md](docs/reviews/sprints-1-2.md).

## Configuração

Correspondência de códigos/sentidos, validação em metros/UTC e relatório diário
estão documentados em [US03-T01 a T04](docs/US03.md). As migrações `us03_0008` e `us03_0009` estendem
os tratados com linha versionada e proveniência. Os vínculos precisam de uma
tabela conferida; configurar o `.env` não cria essa tabela de dados automaticamente.

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

## Exportação diária dos brutos para Parquet (US09-T01)

Instale as dependências, aplique as migrações (o índice `us09_0013` atende à
consulta por dia) e execute, por exemplo,
`python -m app.exportacoes --data 2026-11-18 --saida ./exportacoes`.
O dia é o dia civil de `America/Sao_Paulo` aplicado a `coletado_em`; os instantes
ficam em UTC no arquivo. Use `--lote` para ajustar o tamanho dos lotes (padrão
5000). O exportador lê `registros_gps` em uma transação consistente, paginada por
(`coletado_em`, `id`), mantém os campos tipados e os NULL, e representa `payload`
JSON como texto UTF-8 (forma normalizada pelo tipo JSON do MySQL). O arquivo
inclui fonte e URL de origem, licença CC BY 4.0 com URL, data e período UTC.
A escrita ocorre em arquivo temporário no mesmo diretório e só é publicada
após a conclusão, por substituição atômica, com permissão de leitura `0644`.
Evidência em `docs/US09-T01.md`.

## Migrações do banco

O esquema é versionado com Flask-Migrate (Alembic), em `migrations/versions/`:

- `0001`: esquema base, idêntico ao da `main` no merge 813b9dc.
- `0002`: alinhamento ao diagrama de classes. Não é destrutiva: só cria,
  renomeia, amplia tipos e copia valores.
- `0003`: metadados de `ArquivoArmazenado` e `VersaoGTFS`, com unicidade por hash.
- `0004`: `ObservacaoTratada` separada do bruto, com FK obrigatória/única para
  `registros_gps` e FK opcional para a versão GTFS. A `0009` cria uma tabela de
  snapshots por execução e bruto, sem mudar a relação 1:1 desta classe.
  Evidências e limites em [US12-T01B](docs/US12-T01B.md).
- `0005`: `verificacoes_gtfs`, agenda e resultado da verificação diária do GTFS
  ([US02-T04](docs/US02-T04.md)).
- `0006`: `linhas` e `paradas`, com chave (versão GTFS, código).
- `0007`: `trechos`, com chaves estrangeiras compostas para linha e paradas da
  mesma versão GTFS.
- `0008`: capturas da programação oficial e `partidas_programadas`, com
  procedência e histórico ([US04-T01](docs/US04-T01.md)).
- `0009`: manifesto `ExecucaoProcessamento` e `resultados_reprocessamento`
  (resultado por execução e bruto). Não altera brutos nem `observacoes_tratadas`.
- `us03_0008`: correspondência versionada e proveniência do tratamento.
- `us03_0009`: revisões de correspondência preservando tabelas anteriores.
- `0010_us03_programacao`: integra US03/US04 em uma linha de migração.
- `0011_merge_us12_us03`: une a linha US12 (`0009`) à US03/US04 (`0010`),
  sem repetir DDLs.
- `us29_0012`: lacunas, notificações internas, posse exclusiva e medição de
  confirmação da coleta ([US29](docs/US29.md)).
- `merge_0023`: une as linhas US06 (clima) e US07 (infraestrutura), que partiam
  de `us15_0019`; sem DDL. Sem ela, `db upgrade` para com "Multiple head revisions".

A importação/consulta de partidas é explícita: `python -m app.programacao --help`.
Consulte [US04-T01](docs/US04-T01.md) para fontes, normalização, testes e o
encaminhamento de fallback quando a fonte oficial está indisponível. A geração
por calendário GTFS e horários acima de 24h está documentada em
[US04-T02A](docs/US04-T02A.md) e [US04-T02B](docs/US04-T02B.md).

A consulta de partidas persistidas atende em
`GET /partidas?linha=3762-10&sentido=ida&data=2026-11-19` (também em
`/api/partidas`). Filtros, respostas, schema e evidência em
[US04-T03](docs/US04-T03.md). A rota não gera programação.

A conferência de 30 linhas contra o GTFS real, com dois sentidos e registro das
diferenças, está em [US04-T04](docs/US04-T04.md). O ensaio usa SQLite local e
preserva as pendências de revisão e validação operacional.

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
- `execucoes_processamento` (`ExecucaoProcessamento`): manifesto do período,
  com chave lógica, parâmetros, versões de base, GTFS, código e regra e estados
  de job alinhados ao diagrama. O serviço `app.persistencia.reprocessamento` lê
  por `coletado_em` em ordem crescente de ID, confirma cada lote tratado em
  transação própria e pode retomar um manifesto com erro. O serviço grava apenas
  `resultados_reprocessamento`, com referências ao bruto, à execução e às versões.
  `ObservacaoTratada` continua sendo a visão atual 1:1 mantida pelo MatchingService
  (US03) e não é sobrescrita pelo reprocessamento. Nenhuma etapa do serviço
  atualiza ou apaga `registros_gps`. Limitação: `resultados_reprocessamento.valida`
  é NOT NULL; usar o MatchingService como processador (validade pendente = NULL)
  exige migração revisada.

**US12-T04 — reprodução mensal de indicadores.** O relatório em
[docs/US12-T04.md](docs/US12-T04.md) registra a dependência ainda ausente de
US14/US16, do `IndicatorService` e de um conjunto mensal publicado. Pronta com
essa limitação explícita: os testes sintéticos não são apresentados como prova
de indicadores reais, e o aceite integral com um mês real fica pendente.

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
teste automatizado chama a SPTrans de verdade. Como `app/config.py` carrega o
`.env`, rode com `DATABASE_URL=sqlite:// SPTRANS_TOKEN= python -m pytest` quando o
`.env` apontar para a Aiven (variáveis já definidas não são sobrescritas).
Os testes MySQL opt-in (`US12_MYSQL_TEST_URL_*`, `US29_MYSQL_TEST_URL`,
`US32_MYSQL_ADMIN_URL`) aceitam somente bases locais descartáveis.

## Resultado real da US03 — 19/11/2026

**Meta de 98% atendida: 99,55% (11.253 / 11.304 válidas).** Foram processadas 11.479 posições reais, preservando os brutos. A correção conferiu 194 códigos/sentidos de circuitos fechados e recuperou 587 observações, sem alterar o denominador. As 51 válidas sem associação continuam visíveis.

Bases existentes da Aiven em `0010_us03_programacao`, compatíveis com a entrega US04 da main. **228 testes e 4 verificações MySQL passaram.** [Evidência final](docs/evidencias/US03-2026-11-19.json).

Consultar com o backend iniciado:
`GET /qualidade/correspondencias?data=2026-11-19`

## Coleta resistente a falhas — US29

O worker usa no máximo seis tentativas de consulta, espera crescente, checkpoint
persistente e proteção contra duplicação. Alertas após 10 minutos e recuperação
são enviados ao webhook de `ALERTA_WEBHOOK_URL` (Discord/Slack/Teams) e ficam
na caixa interna compartilhada, com cópia local durante queda do banco.
Executar o worker sob um supervisor que o reinicie (por exemplo systemd
`Restart=always`).

```powershell
python -m app.coleta.bus_collector
python -m app.monitoramento alertas
```

A coleta precisa permanecer em um processo separado do Flask. Para uma amostra
limitada, acrescentar `--ciclos 2`. O estado padrão fica em `instance/coleta/`;
`COLETA_STATE_DIR` permite escolher um volume persistente.

Na amostra real da Aiven, os dois ciclos gravaram em **18,939 s e 16,017 s**.
É uma amostra curta, não uma observação mensal. [Evidência e critérios](docs/evidencias/US29-2026-11-19.json).

## Conferência dos brutos e recuperação — US09-T03 / US32

Publicação diária do dia anterior até 06:00 (São Paulo), decisão confirmada
por Matheus em 20/11/2026. Conferir todos os campos/nulos e o recibo de publicação:

```powershell
python -m scripts.conferir_parquet --publicar
python -m scripts.backup --destino "DIRETORIO_FORA_DO_REPOSITORIO"
python -m scripts.restaurar --backup "DIRETORIO/backup-..." --destino-arquivos "DIRETORIO_NOVO"
```

Restauração exige `RESTORE_DATABASE_URL` local e base nova `marrom_restore_*`;
recusa Aiven e bases existentes. O backup contém banco + originais, hashes e
revisão Alembic. Os agendamentos executam fora do Flask e dependem da máquina
ligada/sessão aberta. [Conferência Parquet](docs/US09-T03.md) ·
[backup diário e ensaio semestral](docs/US32.md).
