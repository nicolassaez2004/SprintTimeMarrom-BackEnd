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

Toda configuração vem de variáveis de ambiente (arquivo `.env`, que fica fora do
Git). Nenhuma senha ou token é escrito no código. Use `.env.example` como modelo.

| Variável | Obrigatória | Padrão | Descrição |
| --- | --- | --- | --- |
| `DATABASE_URL` | sim | — | `mysql+pymysql://usuario:senha@host:3306/banco` |
| `SPTRANS_TOKEN` | para coletar | — | Token da API Olho Vivo |
| `SPTRANS_API_URL` | não | `https://api.olhovivo.sptrans.com.br/v2.1` | URL base da API |
| `GPS_COLLECTION_INTERVAL_SECONDS` | não | `30` | Intervalo do coletor, inteiro de 15 a 60 |
| `FLASK_DEBUG` | não | `false` | Ativa o modo debug do servidor |

A API só abre conexão com o banco quando precisa dele, então `/health` responde
mesmo sem o MySQL estar no ar. A conexão real será conferida na US12-T01B.

## Migrações do banco

O esquema é versionado com Flask-Migrate (Alembic), em `migrations/versions/`:

- `0001`: esquema base, idêntico ao da `main` no merge 813b9dc.
- `0002`: alinhamento ao diagrama de classes. Não é destrutiva: só cria,
  renomeia, amplia tipos e copia valores.

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

- Com horário GPS (`ta`): a mesma posição (prefixo + `horario_gps`) não é
  gravada de novo, nem em ciclos seguintes. O banco também garante isso
  (`uq_registros_gps_prefixo_horario`).
- Sem horário GPS: todos os registros são preservados. Só não se grava duas
  vezes o mesmo objeto repetido dentro de um ciclo. Coordenadas iguais em
  ciclos diferentes não contam como duplicação.

Para contar ciclos em uma janela de tempo:

- ciclos esperados = duração da janela ÷ `GPS_COLLECTION_INTERVAL_SECONDS`
- ciclos gravados = linhas de `execucoes_coleta` com `iniciada_em` na janela

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
