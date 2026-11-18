# SprintTimeMarrom-BackEnd

## Como iniciar

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # configure DATABASE_URL e SPTRANS_TOKEN
python app.py
```

O servidor inicia em `http://127.0.0.1:5000`. Verifique com `GET /health`.

## Coleta de GPS

`OnibusSelecionado` guarda os prefixos e códigos de linha escolhidos. O coletor
consulta cada linha uma vez por ciclo e grava as posições dos veículos
selecionados em `RegistroGPS`, com horário UTC. O primeiro ciclo começa
imediatamente; os próximos começam a cada 30 segundos. Para iniciar o coletor
em outro processo:

```bash
python -m app.services.gps_collector
```

Configure `GPS_COLLECTION_INTERVAL_SECONDS` no `.env` para ajustar o intervalo
(15 a 60 segundos; padrão: 30). O coletor roda em processo separado da API e
usa um lock PostgreSQL para impedir execuções simultâneas no mesmo banco. Cada
ciclo é registrado em `execucoes_coleta`. Aplique as migrações do banco antes
de iniciar o coletor.

Para consultar posições atuais sem gravá-las, use `--localizar` com um prefixo
ou `--linha` com o letreiro da linha:

```bash
python -m app.services.gps_collector --localizar 5810
python -m app.services.gps_collector --linha 5810
```

## Testes

```bash
python -m pytest -v
```
