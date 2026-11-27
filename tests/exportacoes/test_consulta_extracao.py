"""US28-T03A: GET /extracoes/<id> reflete o job persistido e só oferece o
download com o arquivo final publicado no FileStore."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from app import create_app, db
from app.exportacoes import extracoes
from app.exportacoes.extracoes import ExportService, armazenamento, executor_extracao
from app.jobs import JobService
from app.models import ArquivoArmazenado, ExecucaoColeta, ExecucaoProcessamento, PedidoExtracao, RegistroBrutoGPS

UTC = timezone.utc
INICIO = datetime(2026, 11, 1, 3, tzinfo=UTC)
CORPO = {"linha": "33887", "inicio": "2026-11-01T00:00:00-03:00",
         "fim": "2026-11-08T00:00:00-03:00", "formato": "CSV"}
PERIODO = {"inicio": "2026-11-01T03:00:00Z", "fim": "2026-11-08T03:00:00Z"}


def _criar(tmp_path):
    return create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'db.sqlite'}",
                       "EXPORTACOES_DIR": str(tmp_path / "exportacoes")})


@pytest.fixture
def app(tmp_path):
    app = _criar(tmp_path)
    with app.app_context():
        db.create_all()
        ciclo = ExecucaoColeta(iniciada_em=INICIO - timedelta(days=1))
        db.session.add(ciclo)
        db.session.flush()
        for horas in range(5):
            db.session.add(RegistroBrutoGPS(execucao_id=ciclo.id, linha_origem="33887",
                                            coletado_em=INICIO + timedelta(hours=horas)))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def _pedir(client):
    resposta = client.post("/api/extracoes", json=CORPO)
    assert resposta.status_code == 202
    return resposta.get_json()["id"]


def _consultar(client, pedido_id):
    resposta = client.get(f"/api/extracoes/{pedido_id}")
    assert resposta.status_code == 200 and resposta.headers["Cache-Control"] == "no-store"
    return resposta.get_json()


def _worker(app):
    return executor_extracao(app, ExportService(tamanho_lote=2)).executar_um()


def _job(app, pedido_id, **campos):
    with app.app_context():
        job = db.session.get(PedidoExtracao, pedido_id).execucao
        for campo, valor in campos.items():
            setattr(job, campo, valor)
        db.session.commit()


def armazenamento_dir(app):
    with app.app_context():
        return armazenamento().diretorio


def _caminho_final(app, pedido_id):
    with app.app_context():
        arquivo = db.session.get(PedidoExtracao, pedido_id).arquivo
        return armazenamento().diretorio / f"{arquivo.sha256}.csv"


def test_aguardando_preserva_pedido_e_nao_tem_link(client):
    pedido_id = _pedir(client)
    corpo = _consultar(client, pedido_id)

    assert (corpo["id"], corpo["estado"], corpo["linha"], corpo["periodo"], corpo["formato"]) == (
        pedido_id, "aguardando", "33887", PERIODO, "CSV")
    assert corpo["arquivo"] is None and corpo["erro"] is None and corpo["mensagem"] is None
    assert corpo["progresso"] is None  # nada registrado ainda: sem progresso inventado
    assert client.get(f"/api/extracoes/{pedido_id}/arquivo").status_code == 409


def test_processando_mostra_progresso_registrado_sem_link_nem_temporario(app, client, monkeypatch):
    pedido_id = _pedir(client)
    vistos = []
    original = ExportService._progresso

    def espiar(self, job_id, token, registros):
        original(self, job_id, token, registros)
        corpo = _consultar(client, pedido_id)
        baixar = client.get(f"/api/extracoes/{pedido_id}/arquivo")
        temporarios = list(armazenamento().diretorio.glob(".extracao-*.tmp"))
        vistos.append((corpo["estado"], corpo["arquivo"], corpo["progresso"]["registros"],
                       baixar.status_code, baixar.get_json()["erro"]["estado"], len(temporarios)))
    monkeypatch.setattr(ExportService, "_progresso", espiar)

    assert _worker(app)[1] == "PRONTO"

    # Temporário existe em disco, mas nunca é anunciado nem baixável.
    assert vistos and all(v[:2] == ("processando", None) and v[3:] == (409, "processando", 1)
                          for v in vistos)
    assert [v[2] for v in vistos] == [2, 4, 5, 5]  # registros reais, sem percentual inventado
    assert "percentual" not in str(vistos)


def test_worker_atualiza_para_pronto_com_link_valido(app, client):
    pedido_id = _pedir(client)
    assert _consultar(client, pedido_id)["estado"] == "aguardando"

    assert _worker(app)[1] == "PRONTO"

    corpo = _consultar(client, pedido_id)
    assert (corpo["id"], corpo["estado"], corpo["linha"], corpo["periodo"]) == (
        pedido_id, "pronto", "33887", PERIODO)
    arquivo = corpo["arquivo"]
    assert arquivo["download"] == f"/api/extracoes/{pedido_id}/arquivo"
    assert arquivo["nome"].startswith("brutos_linha_33887_") and arquivo["nome"].endswith(".csv")
    baixado = client.get(arquivo["download"])
    assert baixado.status_code == 200 and baixado.mimetype == "text/csv"
    assert "attachment" in baixado.headers["Content-Disposition"]
    assert sha256(baixado.data).hexdigest() == arquivo["sha256"]
    assert len(baixado.data) == arquivo["tamanho_bytes"]
    # Caminho interno, chave e token nunca aparecem.
    assert str(armazenamento_dir(app)) not in str(corpo) and "chave" not in arquivo


def test_erro_esgotado_tem_mensagem_sem_link_e_sem_detalhe_interno(app, client):
    pedido_id = _pedir(client)
    _job(app, pedido_id, estado="ERRO", tentativas=JobService.LIMITE_TENTATIVAS, erro="OperationalError")

    corpo = _consultar(client, pedido_id)

    assert (corpo["estado"], corpo["erro"], corpo["arquivo"]) == ("erro", "OperationalError", None)
    assert "novo pedido" in corpo["mensagem"]
    assert not {"reserva_token", "chave_logica", "historico"} & set(corpo)
    assert client.get(f"/api/extracoes/{pedido_id}/arquivo").status_code == 409


def test_erro_com_tentativas_restantes_continua_aguardando(app, client):
    pedido_id = _pedir(client)
    _job(app, pedido_id, estado="ERRO", tentativas=1, erro="OSError")

    assert _consultar(client, pedido_id)["estado"] == "aguardando"


def test_job_pronto_com_arquivo_ausente_nao_e_anunciado(app, client):
    pedido_id = _pedir(client)
    assert _worker(app)[1] == "PRONTO"
    caminho = _caminho_final(app, pedido_id)
    caminho.unlink()

    corpo = _consultar(client, pedido_id)

    assert (corpo["estado"], corpo["erro"], corpo["arquivo"]) == ("erro", "ARQUIVO_INDISPONIVEL", None)
    assert client.get(f"/api/extracoes/{pedido_id}/arquivo").status_code == 409


def test_arquivo_truncado_nao_e_anunciado(app, client):
    pedido_id = _pedir(client)
    assert _worker(app)[1] == "PRONTO"
    caminho = _caminho_final(app, pedido_id)
    caminho.write_bytes(caminho.read_bytes()[:-1])

    assert _consultar(client, pedido_id)["estado"] == "erro"


def test_arquivo_adulterado_com_mesmo_tamanho_nao_e_baixado(app, client):
    """A consulta faz a checagem leve; o download confere o hash completo."""
    pedido_id = _pedir(client)
    assert _worker(app)[1] == "PRONTO"
    caminho = _caminho_final(app, pedido_id)
    caminho.write_bytes(b"x" * caminho.stat().st_size)

    resposta = client.get(f"/api/extracoes/{pedido_id}/arquivo")

    assert resposta.status_code == 404
    assert resposta.get_json()["erro"]["codigo"] == "ARQUIVO_INDISPONIVEL"
    assert str(caminho) not in resposta.get_data(as_text=True)


def test_registro_apontando_para_temporario_nao_e_publicado(app, client):
    pedido_id = _pedir(client)
    assert _worker(app)[1] == "PRONTO"
    with app.app_context():
        arquivo = db.session.get(PedidoExtracao, pedido_id).arquivo
        temporario = armazenamento().diretorio / f".extracao-{arquivo.sha256}.tmp"
        temporario.write_bytes(b"0" * arquivo.tamanho_bytes)
        arquivo.chave = f"extracoes/{temporario.name}"
        db.session.commit()

    assert _consultar(client, pedido_id)["estado"] == "erro"


@pytest.mark.parametrize("pedido_id", [
    "inexistente", "00000000-0000-0000-0000-000000000000",
    "00000000-0000-0000-0000-00000000000G", "' OR 1=1 --", "a" * 300,
])
def test_identificador_inexistente_ou_invalido_responde_404(client, pedido_id):
    for url in (f"/api/extracoes/{pedido_id}", f"/extracoes/{pedido_id}/arquivo"):
        resposta = client.get(url)
        assert resposta.status_code == 404
        assert resposta.get_json()["erro"]["codigo"] == "PEDIDO_NAO_ENCONTRADO"


def test_estado_persistido_sobrevive_ao_reinicio(app, client, tmp_path):
    pronto = _pedir(client)
    assert _worker(app)[1] == "PRONTO"
    aguardando = client.post("/api/extracoes", json={**CORPO, "formato": "PARQUET"}).get_json()["id"]
    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    reiniciado = _criar(tmp_path).test_client()  # novo processo Flask, sem memória anterior

    assert _consultar(reiniciado, aguardando)["estado"] == "aguardando"
    corpo = _consultar(reiniciado, pronto)
    assert corpo["estado"] == "pronto" and reiniciado.get(corpo["arquivo"]["download"]).status_code == 200


def test_consulta_nao_inicia_job_nem_gera_arquivo(app, client, monkeypatch):
    pedido_id = _pedir(client)

    def proibido(*_, **__):
        raise AssertionError("a consulta não pode executar trabalho")
    for alvo, nome in ((extracoes, "escrever_brutos"), (ExportService, "gerar"),
                       (ExportService, "publicar"), (JobService, "reservar")):
        monkeypatch.setattr(alvo, nome, proibido)

    for _ in range(3):
        assert _consultar(client, pedido_id)["estado"] == "aguardando"
        assert client.get(f"/api/extracoes/{pedido_id}/arquivo").status_code == 409

    with app.app_context():
        job = ExecucaoProcessamento.query.one()
        assert (job.estado, job.tentativas, job.reserva_token, job.iniciada_em) == ("AGUARDANDO", 0, None, None)
    assert not list(armazenamento_dir(app).iterdir())


def test_tentativa_que_falhou_nao_vira_progresso_nem_no_erro_final(app, client):
    """Review US28: ERRO com tentativas restantes é 'aguardando' sem a contagem antiga."""
    pedido_id = _pedir(client)
    historico = [{"tentativa": 1, "estado": "ERRO", "erro": "OSError", "registros": 4,
                  "progresso_em": "2026-11-20T10:00:00Z", "fim": "2026-11-20T10:00:01Z"}]
    _job(app, pedido_id, estado="ERRO", tentativas=1, erro="OSError", historico=historico)
    assert (_consultar(client, pedido_id)["estado"], _consultar(client, pedido_id)["progresso"]) == (
        "aguardando", None)

    _job(app, pedido_id, tentativas=JobService.LIMITE_TENTATIVAS)
    assert (_consultar(client, pedido_id)["estado"], _consultar(client, pedido_id)["progresso"]) == (
        "erro", None)


def test_arquivo_compartilhado_e_baixado_com_o_nome_do_proprio_pedido(app, client):
    """Review US28: CSVs idênticos (ex.: linhas sem registros) reutilizam o mesmo
    ArquivoArmazenado; o nome exibido e baixado continua sendo o do pedido."""
    ids = [client.post("/api/extracoes", json={**CORPO, "linha": linha}).get_json()["id"]
           for linha in ("901", "902")]
    assert _worker(app)[1] == _worker(app)[1] == "PRONTO"
    with app.app_context():
        assert ArquivoArmazenado.query.count() == 1  # mesmo conteúdo, um objeto
    for pedido_id, linha in zip(ids, ("901", "902")):
        arquivo = _consultar(client, pedido_id)["arquivo"]
        assert arquivo["nome"] == f"brutos_linha_{linha}_20261101T0300Z_20261108T0300Z.csv"
        baixado = client.get(arquivo["download"])
        assert arquivo["nome"] in baixado.headers["Content-Disposition"]
