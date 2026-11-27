"""US28-T02: geração do arquivo de extração pelo worker, fora do Flask."""
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pytest

from app import create_app, db
from app.exportacoes import extracoes
from app.exportacoes.extracoes import ExportService, armazenamento, executor_extracao
from app.exportacoes.parquet import _SCHEMA
from app.jobs.executor import Executor
from app.models import (ArquivoArmazenado, ExecucaoColeta, ExecucaoProcessamento, PedidoExtracao,
                        RegistroBrutoGPS)
from app.storage import FileStore, hash_arquivo

RAIZ = Path(__file__).resolve().parents[2]
UTC = timezone.utc
INICIO = datetime(2026, 11, 1, 3, tzinfo=UTC)
FIM = datetime(2026, 11, 8, 3, tzinfo=UTC)
LINHA = "33887"
CORPO = {"linha": LINHA, "inicio": "2026-11-01T00:00:00-03:00",
         "fim": "2026-11-08T00:00:00-03:00", "formato": "PARQUET"}
ESCREVER = extracoes.escrever_brutos
DEPOIS = lambda: datetime.now(UTC) + timedelta(hours=1)  # reservas anteriores vencidas


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'db.sqlite'}",
                      "EXPORTACOES_DIR": str(tmp_path / "exportacoes")})
    with app.app_context():
        db.create_all()
        ciclo = ExecucaoColeta(iniciada_em=INICIO - timedelta(days=1))
        db.session.add(ciclo)
        db.session.flush()

        def bruto(instante, linha=LINHA, **campos):
            db.session.add(RegistroBrutoGPS(execucao_id=ciclo.id, coletado_em=instante,
                                            linha_origem=linha, payload={"cl": linha}, **campos))

        bruto(INICIO - timedelta(microseconds=1))  # antes do período
        bruto(INICIO, prefixo_origem="11001", latitude=-23.5, longitude=-46.6,
              horario_gps=INICIO - timedelta(seconds=20))
        for horas in range(1, 6):
            bruto(INICIO + timedelta(hours=horas), prefixo_origem=f"1100{horas}")
        bruto(FIM - timedelta(microseconds=1), sentido_origem="2")
        bruto(FIM)  # fim exclusivo
        bruto(INICIO + timedelta(hours=2), linha="1111")  # outra linha
        bruto(INICIO + timedelta(hours=3), linha=None)
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def _esperados(app):
    with app.app_context():
        R = RegistroBrutoGPS
        return [i for (i,) in db.session.query(R.id).filter(
            R.linha_origem == LINHA, R.coletado_em >= INICIO, R.coletado_em < FIM)
            .order_by(R.coletado_em, R.id)]


def _pedir(app, **corpo):
    resposta = app.test_client().post("/api/extracoes", json={**CORPO, **corpo})
    assert resposta.status_code == 202
    return resposta.get_json()["id"]


def _estado(app, pedido_id):
    return app.test_client().get(f"/api/extracoes/{pedido_id}").get_json()


def _worker(app, servico=None, **opcoes):
    return executor_extracao(app, servico or ExportService(tamanho_lote=2), **opcoes)


def _loja(app):
    with app.app_context():
        return armazenamento().diretorio


def _finais(app):
    return sorted(p.name for p in _loja(app).iterdir() if not p.name.startswith("."))


def _temporarios(app):
    return sorted(p.name for p in _loja(app).glob(".extracao-*.tmp"))


def _arquivo(app, pedido_id):
    with app.app_context():
        pedido = db.session.get(PedidoExtracao, pedido_id)
        arquivo = pedido.arquivo
        caminho = armazenamento().caminho_extracao(arquivo.chave, arquivo.sha256)
        return caminho, arquivo.sha256, pedido.execucao.historico


def test_worker_publica_somente_linha_periodo_e_snapshot_com_metadados_da_us09(app):
    pedido_id = _pedir(app)
    with app.app_context():  # gravado depois do pedido: fora do snapshot
        ciclo_id = db.session.query(ExecucaoColeta.id).scalar()
        db.session.add(RegistroBrutoGPS(execucao_id=ciclo_id, linha_origem=LINHA,
                                        coletado_em=INICIO + timedelta(hours=4, minutes=1)))
        db.session.commit()
    with app.app_context():
        R = RegistroBrutoGPS
        snapshot = db.session.get(PedidoExtracao, pedido_id).execucao.bruto_id_maximo
        esperados = [i for (i,) in db.session.query(R.id).filter(
            R.linha_origem == LINHA, R.coletado_em >= INICIO, R.coletado_em < FIM, R.id <= snapshot)
            .order_by(R.coletado_em, R.id)]
        brutos_antes = [(r.id, r.payload, r.coletado_em) for r in R.query.order_by(R.id)]

    assert _worker(app).executar_um()[1] == "PRONTO"

    caminho, digest, historico = _arquivo(app, pedido_id)
    tabela = pq.read_table(caminho)
    assert tabela.column("id").to_pylist() == esperados and len(esperados) == 7
    assert set(tabela.column("linha_origem").to_pylist()) == {LINHA}
    assert all(INICIO <= t < FIM for t in tabela.column("coletado_em").to_pylist())
    assert tabela.schema.equals(_SCHEMA)  # mesmas colunas e tipos da US09
    meta = pq.read_metadata(caminho).metadata
    assert meta[b"license"] == b"CC BY 4.0" and meta[b"source"].startswith(b"SPTrans Olho Vivo")
    assert meta[b"request_id"] == pedido_id.encode() and meta[b"line"] == LINHA.encode()
    assert hash_arquivo(caminho)[0] == digest
    corpo = _estado(app, pedido_id)
    assert corpo["estado"] == "PRONTO" and corpo["arquivo"]["sha256"] == digest
    assert corpo["progresso"]["registros"] == 7
    assert historico[-1]["estado"] == "PRONTO" and historico[-1]["registros"] == 7
    with app.app_context():
        pedido = db.session.get(PedidoExtracao, pedido_id)
        assert (pedido.execucao.estado, pedido.arquivo.mime_type) == ("PRONTO", "application/vnd.apache.parquet")
        assert [(r.id, r.payload, r.coletado_em) for r in RegistroBrutoGPS.query.order_by(
            RegistroBrutoGPS.id) if r.id <= snapshot] == [b for b in brutos_antes if b[0] <= snapshot]


def test_csv_leva_colunas_da_us09_mais_fonte_e_licenca_em_cada_linha(app):
    pedido_id = _pedir(app, formato="CSV")

    assert _worker(app).executar_um()[1] == "PRONTO"

    caminho, _, _ = _arquivo(app, pedido_id)
    tabela = pacsv.read_csv(caminho)
    assert tabela.column_names == [c.name for c in _SCHEMA] + ["fonte", "licenca"]
    assert tabela.column("id").to_pylist() == _esperados(app)
    assert set(tabela.column("licenca").to_pylist()) == {"CC BY 4.0"}
    assert caminho.suffix == ".csv"


def test_le_em_lotes_registra_progresso_e_nada_aparece_antes_de_publicar(app, monkeypatch):
    pedido_id = _pedir(app)
    vistos = []
    original = ExportService._progresso

    def espiar(self, job_id, token, registros):
        original(self, job_id, token, registros)
        corpo = _estado(app, pedido_id)
        vistos.append((registros, corpo["estado"], corpo["arquivo"], corpo["progresso"]["registros"],
                       _finais(app), len(_temporarios(app))))
    monkeypatch.setattr(ExportService, "_progresso", espiar)
    limites = []
    monkeypatch.setattr(extracoes, "escrever_brutos", lambda *a, **k: limites.append(
        k["tamanho_lote"]) or ESCREVER(*a, **k))

    assert _worker(app).executar_um()[1] == "PRONTO"

    assert limites == [2]
    # Lotes de 2 (7 registros) e a confirmação final após a validação.
    assert [v[0] for v in vistos] == [2, 4, 6, 7, 7]
    for registros, estado, arquivo, progresso, finais, temporarios in vistos:
        assert (estado, arquivo, finais, temporarios) == ("PROCESSANDO", None, [], 1)
        assert progresso == registros
    assert _temporarios(app) == [] and len(_finais(app)) == 1


def test_falha_na_gravacao_vira_erro_recuperavel_sem_temporario(app, monkeypatch):
    pedido_id = _pedir(app)
    chamadas = []

    def quebrar(self, job_id, token, registros):
        chamadas.append(registros)
        raise OSError("disco cheio")
    monkeypatch.setattr(ExportService, "_progresso", quebrar)

    assert _worker(app).executar_um()[1] == "ERRO"

    corpo = _estado(app, pedido_id)
    assert (corpo["estado"], corpo["tentativas"], corpo["arquivo"]) == ("AGUARDANDO", 1, None)
    with app.app_context():
        job = db.session.get(PedidoExtracao, pedido_id).execucao
        assert (job.estado, job.erro, job.historico[-1]["erro"]) == ("ERRO", "OSError", "OSError")
        assert ArquivoArmazenado.query.count() == 0
    assert _temporarios(app) == [] and _finais(app) == []
    monkeypatch.undo()
    assert _worker(app).executar_um()[1] == "PRONTO"
    assert _estado(app, pedido_id)["estado"] == "PRONTO"


def test_arquivo_invalido_nao_e_publicado(app, monkeypatch):
    pedido_id = _pedir(app)
    monkeypatch.setattr(ExportService, "_validar", staticmethod(
        lambda *_: (_ for _ in ()).throw(extracoes.ArquivoInvalido("truncado"))))

    assert _worker(app).executar_um()[1] == "ERRO"
    assert _estado(app, pedido_id)["arquivo"] is None
    assert _temporarios(app) == [] and _finais(app) == []


def test_falha_na_publicacao_desfaz_vinculo_e_retomada_nao_duplica(app, monkeypatch):
    pedido_id = _pedir(app)

    def indisponivel(self, chave, digest):
        raise FileNotFoundError("volume desmontado")
    monkeypatch.setattr(FileStore, "caminho_extracao", indisponivel)

    assert _worker(app).executar_um()[1] == "ERRO"
    with app.app_context():
        pedido = db.session.get(PedidoExtracao, pedido_id)
        assert pedido.arquivo_id is None and ArquivoArmazenado.query.count() == 0
        assert pedido.execucao.estado == "ERRO"
    orfao = _finais(app)  # objeto ligado mas nunca anunciado (sem ArquivoArmazenado)
    assert _estado(app, pedido_id)["arquivo"] is None and _temporarios(app) == []

    monkeypatch.undo()
    assert _worker(app).executar_um()[1] == "PRONTO"
    # Conteúdo determinístico: a retomada reconhece o mesmo objeto, sem segundo arquivo.
    assert _finais(app) == orfao and len(orfao) == 1
    with app.app_context():
        assert ArquivoArmazenado.query.count() == 1


def test_worker_antigo_nao_publica_depois_de_perder_a_reserva(app):
    pedido_id = _pedir(app)
    novo = _worker(app, relogio=DEPOIS)
    resultado_novo = []
    servico = ExportService(tamanho_lote=2)

    def gerar_e_perder_posse(job):
        gerado = servico.gerar(job)  # arquivo completo, ainda não publicado
        resultado_novo.append(novo.executar_um())  # reserva vence; outro worker assume
        return gerado

    antigo = Executor(app, extracoes.TIPO, gerar_e_perder_posse, servico.publicar)

    assert antigo.executar_um()[1] == "ERRO"  # PossePerdida: nada publicado por ele
    assert resultado_novo[0][1] == "PRONTO"
    with app.app_context():
        job = db.session.get(PedidoExtracao, pedido_id).execucao
        assert job.estado == "PRONTO" and job.tentativas == 2
        assert [t["estado"] for t in job.historico] == ["INTERROMPIDA", "PRONTO"]
        assert job.historico[0]["erro"] == "ReservaExpirada"
        assert ArquivoArmazenado.query.count() == 1
    assert len(_finais(app)) == 1 and _temporarios(app) == []
    assert _estado(app, pedido_id)["estado"] == "PRONTO"


def test_worker_antigo_para_de_gravar_ao_perder_a_reserva(app):
    pedido_id = _pedir(app)
    servico = ExportService(tamanho_lote=2)
    original = servico._progresso

    def roubar_posse(job_id, token, registros):
        with app.app_context():
            job = db.session.get(ExecucaoProcessamento, job_id)
            job.reserva_token = "outro-worker-0000-0000-000000000000"
            db.session.commit()
        original(job_id, token, registros)
    servico._progresso = roubar_posse

    assert _worker(app, servico).executar_um()[1] == "ERRO"
    with app.app_context():
        assert db.session.get(PedidoExtracao, pedido_id).arquivo_id is None
    assert _finais(app) == [] and _temporarios(app) == []


@pytest.mark.parametrize("falhar", [False, True])
def test_sessoes_e_conexoes_sao_devolvidas(app, monkeypatch, falhar):
    _pedir(app)
    if falhar:
        monkeypatch.setattr(ExportService, "_validar", staticmethod(
            lambda *_: (_ for _ in ()).throw(extracoes.ArquivoInvalido("x"))))

    _worker(app).executar_um()

    with app.app_context():
        assert db.engine.pool.checkedout() == 0


def _ambiente(app, tmp_path):
    return {**os.environ, "DATABASE_URL": app.config["SQLALCHEMY_DATABASE_URI"],
            "MYSQL_SSL_CA": "", "EXPORTACOES_DIR": app.config["EXPORTACOES_DIR"],
            "PYTHONPATH": str(RAIZ)}


def test_fluxo_completo_com_worker_em_processo_separado(app, tmp_path):
    """POST → pedido + job → `python -m app.jobs` (outro processo) → GET."""
    pedido_id = _pedir(app)
    assert _estado(app, pedido_id)["estado"] == "AGUARDANDO"

    processo = subprocess.run(
        [sys.executable, "-m", "app.jobs", "--tipo", "EXTRACAO_BRUTOS", "--uma-vez"],
        cwd=RAIZ, env=_ambiente(app, tmp_path), capture_output=True, text=True, timeout=120)

    assert processo.returncode == 0, processo.stderr
    corpo = _estado(app, pedido_id)
    assert corpo["estado"] == "PRONTO" and corpo["progresso"]["registros"] == 7
    caminho, _, _ = _arquivo(app, pedido_id)
    assert pq.read_table(caminho).column("id").to_pylist() == _esperados(app)


INTERROMPER = textwrap.dedent("""
    import os, sys
    from app import create_app
    from app.exportacoes.extracoes import ExportService, executor_extracao
    app = create_app({"SQLALCHEMY_DATABASE_URI": sys.argv[1], "EXPORTACOES_DIR": sys.argv[2]})
    servico = ExportService(tamanho_lote=2)
    original = servico._progresso
    def progresso(job_id, token, registros):
        original(job_id, token, registros)
        if registros >= 4:
            os._exit(9)  # morte abrupta: sem finally, sem rollback, sem limpeza
    servico._progresso = progresso
    executor_extracao(app, servico).executar_um()
""")


def test_interrupcao_e_retomada_sem_publicacao_parcial_ou_duplicada(app, tmp_path):
    pedido_id = _pedir(app)

    morto = subprocess.run([sys.executable, "-c", INTERROMPER, app.config["SQLALCHEMY_DATABASE_URI"],
                            app.config["EXPORTACOES_DIR"]], cwd=RAIZ, env=_ambiente(app, tmp_path),
                           capture_output=True, text=True, timeout=120)

    # 1. Parte do arquivo foi gravada, mas nada está disponível.
    assert morto.returncode == 9, morto.stderr
    parcial = _temporarios(app)
    assert len(parcial) == 1 and (_loja(app) / parcial[0]).stat().st_size > 0
    corpo = _estado(app, pedido_id)
    assert (corpo["estado"], corpo["arquivo"], corpo["progresso"]["registros"]) == ("PROCESSANDO", None, 4)
    assert _finais(app) == []

    # 2. Retomada depois de a reserva vencer.
    assert _worker(app, relogio=DEPOIS).executar_um()[1] == "PRONTO"
    caminho, digest, historico = _arquivo(app, pedido_id)
    assert pq.read_table(caminho).column("id").to_pylist() == _esperados(app)
    assert _temporarios(app) == [] and _finais(app) == [caminho.name]
    assert [(t["tentativa"], t["estado"], t["erro"], t["registros"]) for t in historico] == [
        (1, "INTERROMPIDA", "ReservaExpirada", 4), (2, "PRONTO", None, 7)]

    # 3. Nada a refazer: sem novo job, arquivo ou pedido.
    assert _worker(app, relogio=DEPOIS).executar_um() is None
    assert _pedir(app) == pedido_id and _finais(app) == [caminho.name]
    with app.app_context():
        assert (ArquivoArmazenado.query.count(), ExecucaoProcessamento.query.count()) == (1, 1)
    assert hash_arquivo(caminho)[0] == digest
