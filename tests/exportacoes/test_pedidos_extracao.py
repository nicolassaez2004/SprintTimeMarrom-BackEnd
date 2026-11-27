"""US28-T01: pedidos de extração registrados com job durável e resposta 202."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.exportacoes import extracoes
from app.exportacoes.extracoes import TIPO
from app.jobs import JobService
from app.models import ArquivoArmazenado, ExecucaoProcessamento, PedidoExtracao

CORPO = {"linha": "33887", "inicio": "2026-11-01T00:00:00-03:00",
         "fim": "2026-11-08T00:00:00-03:00", "formato": "parquet"}


def _app(uri):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture
def app(tmp_path):
    app = _app(f"sqlite:///{tmp_path / 'extracoes.db'}")
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def _contagens(app):
    with app.app_context():
        return PedidoExtracao.query.count(), ExecucaoProcessamento.query.count()


def test_pedido_valido_responde_202_com_id_opaco_sem_processar(app, client):
    resposta = client.post("/api/extracoes", json=CORPO)

    assert resposta.status_code == 202
    corpo = resposta.get_json()
    assert len(corpo["id"]) == 36 and corpo["id"].count("-") == 4
    assert corpo["estado"] == "AGUARDANDO" and corpo["erro"] is None
    assert corpo["formato"] == "PARQUET" and corpo["linha"] == "33887"
    assert corpo["periodo"] == {"inicio": "2026-11-01T03:00:00Z", "fim": "2026-11-08T03:00:00Z"}
    assert resposta.headers["Location"] == corpo["consulta"] == f"/api/extracoes/{corpo['id']}"
    # Nada de chave lógica, token, job interno ou caminho de arquivo na API.
    assert not {"chave_logica", "reserva_token", "execucao_processamento_id", "arquivo"} & set(corpo)
    with app.app_context():
        job = db.session.get(PedidoExtracao, corpo["id"]).execucao
        # Não aguardou a geração: job intacto e nenhum arquivo criado.
        assert (job.tipo, job.estado, job.tentativas, job.iniciada_em) == (TIPO, "AGUARDANDO", 0, None)
        assert ArquivoArmazenado.query.count() == 0


def test_pedido_e_job_vinculados_e_recuperaveis(app, client):
    pedido_id = client.post("/extracoes", json=CORPO).get_json()["id"]

    consulta = client.get(f"/extracoes/{pedido_id}")
    assert consulta.status_code == 200
    assert consulta.get_json()["id"] == pedido_id
    with app.app_context():
        pedido = db.session.get(PedidoExtracao, pedido_id)
        job = pedido.execucao
        assert job.parametros["linha"] == "33887" and job.parametros["formato"] == "PARQUET"
        assert (job.periodo_inicio, job.periodo_fim) == (pedido.inicio_periodo, pedido.fim_periodo)
        assert job.versao_base == pedido.versao_base == "registros_gps:id<=0"
        assert job.chave_logica.startswith("extracao-us28:")


@pytest.mark.parametrize("campo, valor", [
    ("linha", "8000-10"), ("linha", ""), ("linha", 33887), ("linha", None),
    ("inicio", "2026-11-01"), ("inicio", "2026-11-01T00:00:00"), ("inicio", "ontem"),
    ("fim", "2026-10-31T00:00:00-03:00"),  # fim antes do início
    ("fim", "2026-12-15T00:00:00-03:00"),  # mais de 31 dias
    ("formato", "xlsx"), ("extra", "x"),
])
def test_entrada_invalida_nao_cria_registros(app, client, campo, valor):
    resposta = client.post("/api/extracoes", json={**CORPO, campo: valor})

    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["campo"] == campo
    assert _contagens(app) == (0, 0)


def test_periodo_ainda_aberto_e_recusado(app, client):
    agora = datetime.now(timezone.utc)
    corpo = {**CORPO, "inicio": (agora - timedelta(hours=1)).isoformat(), "fim": agora.isoformat()}

    assert client.post("/api/extracoes", json=corpo).status_code == 400
    assert client.post("/api/extracoes", data="nao-json", content_type="application/json").status_code == 400
    assert _contagens(app) == (0, 0)


def test_falha_ao_gravar_pedido_desfaz_o_job(app, client, monkeypatch):
    def falhar(*_):
        raise OperationalError("INSERT pedidos_extracao", {}, Exception("disco cheio"))
    monkeypatch.setattr(extracoes.ExportService, "_novo", staticmethod(falhar))

    resposta = client.post("/api/extracoes", json=CORPO)

    assert resposta.status_code == 503
    assert "disco" not in resposta.get_data(as_text=True)
    assert _contagens(app) == (0, 0)


def test_falha_no_commit_nao_deixa_registro_parcial(app, client, monkeypatch):
    original = db.session.commit

    def commit_falho():
        db.session.rollback()
        raise OperationalError("COMMIT", {}, Exception("conexão perdida"))
    with app.app_context():
        monkeypatch.setattr(db.session, "commit", commit_falho)
        assert client.post("/api/extracoes", json=CORPO).status_code == 503
        monkeypatch.setattr(db.session, "commit", original)
    assert _contagens(app) == (0, 0)


def test_reenvio_idempotente_nao_duplica(app, client):
    primeiro = client.post("/api/extracoes", json=CORPO).get_json()
    # Mesmo período com outro fuso e formato em outra caixa: mesmo pedido.
    mesmo = {**CORPO, "inicio": "2026-11-01T03:00:00+00:00", "formato": "PARQUET"}
    segundo = client.post("/api/extracoes", json=mesmo)

    assert segundo.status_code == 202 and segundo.get_json()["id"] == primeiro["id"]
    assert _contagens(app) == (1, 1)
    outro = client.post("/api/extracoes", json={**CORPO, "formato": "CSV"}).get_json()
    assert outro["id"] != primeiro["id"]
    assert _contagens(app) == (2, 2)


def test_erro_esgotado_abre_nova_geracao(app, client):
    primeiro = client.post("/api/extracoes", json=CORPO).get_json()["id"]
    with app.app_context():
        job = db.session.get(PedidoExtracao, primeiro).execucao
        job.estado, job.tentativas, job.erro = "ERRO", JobService.LIMITE_TENTATIVAS, "TimeoutError"
        db.session.commit()
    assert client.get(f"/api/extracoes/{primeiro}").get_json()["erro"] == "TimeoutError"

    novo = client.post("/api/extracoes", json=CORPO).get_json()

    assert novo["id"] != primeiro and novo["estado"] == "AGUARDANDO"
    assert _contagens(app) == (2, 2)
    assert client.get(f"/api/extracoes/{primeiro}").get_json()["estado"] == "ERRO"


@pytest.mark.parametrize("estado, tentativas, arquivo, publico", [
    ("AGUARDANDO", 0, False, "AGUARDANDO"),
    ("PROCESSANDO", 1, False, "PROCESSANDO"),
    ("ERRO", 1, False, "AGUARDANDO"),  # será retomado pelo worker
    ("ERRO", JobService.LIMITE_TENTATIVAS, False, "ERRO"),
    ("PRONTO", 1, False, "ERRO"),  # pronto sem arquivo nunca é anunciado
    ("PRONTO", 1, True, "PRONTO"),
])
def test_estado_publico_deriva_do_job(app, client, estado, tentativas, arquivo, publico):
    pedido_id = client.post("/api/extracoes", json=CORPO).get_json()["id"]
    with app.app_context():
        pedido = db.session.get(PedidoExtracao, pedido_id)
        pedido.execucao.estado, pedido.execucao.tentativas = estado, tentativas
        if arquivo:
            pedido.arquivo = ArquivoArmazenado(chave="extracoes/x.parquet", nome_original="x.parquet",
                                               mime_type="application/vnd.apache.parquet",
                                               tamanho_bytes=1, sha256="0" * 64)
        db.session.commit()

    assert client.get(f"/api/extracoes/{pedido_id}").get_json()["estado"] == publico


@pytest.mark.parametrize("pedido_id", ["inexistente", "00000000-0000-0000-0000-000000000000"])
def test_pedido_inexistente_responde_404(client, pedido_id):
    resposta = client.get(f"/api/extracoes/{pedido_id}")

    assert resposta.status_code == 404
    assert resposta.get_json()["erro"]["codigo"] == "PEDIDO_NAO_ENCONTRADO"


def test_reinicio_preserva_pedido_e_trabalho_pendente_sem_worker(tmp_path):
    """Sem fila externa: o job confirmado no banco é o trabalho pendente.

    Nenhum worker roda aqui (indisponível); após "reiniciar" o processo, o
    pedido continua consultável e o worker o encontra como pendente.
    """
    uri = f"sqlite:///{tmp_path / 'reinicio.db'}"
    primeiro = _app(uri)
    pedido_id = primeiro.test_client().post("/api/extracoes", json=CORPO).get_json()["id"]
    with primeiro.app_context():
        db.engine.dispose()

    reiniciado = _app(uri)
    corpo = reiniciado.test_client().get(f"/api/extracoes/{pedido_id}").get_json()
    assert corpo["estado"] == "AGUARDANDO"
    with reiniciado.app_context():
        pendentes = JobService().pendentes(TIPO)
        assert [p.id for p in pendentes] == [db.session.get(PedidoExtracao, pedido_id).execucao_processamento_id]
        db.engine.dispose()


def test_banco_indisponivel_na_consulta_responde_503(client, monkeypatch):
    def falhar(_):
        raise OperationalError("SELECT", {}, Exception("senha=segredo"))
    monkeypatch.setattr(extracoes.ExportService, "obter", staticmethod(falhar))

    resposta = client.get("/api/extracoes/00000000-0000-0000-0000-000000000000")

    assert resposta.status_code == 503 and "segredo" not in resposta.get_data(as_text=True)


def test_migracao_us28_e_a_cabeca_da_cadeia():
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "migrations"))
    assert scripts.get_heads() == ["us28_0028"]
    assert scripts.get_revision("us28_0028").down_revision == "us21_0027"
