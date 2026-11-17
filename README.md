# SprintTimeMarrom-BackEnd

## Como iniciar

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # preencha DATABASE_URL (MySQL) e SPTRANS_TOKEN
python app.py
```

O servidor sobe em `http://127.0.0.1:5000`. Para verificar: `GET /health` deve retornar `{"status": "ok"}`.

## Testes

```bash
python -m pytest -v
```
