import hashlib
import io
import zipfile
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app, db
from app.gtfs.service import GTFSValidationError, capturar_gtfs
from app.models import ArquivoArmazenado, VersaoGTFS


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "GTFS_STORAGE_DIR": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()


def _zip_gtfs(conteudo="v1"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as arquivo:
        for nome in (
            "agency.txt", "stops.txt", "routes.txt", "trips.txt",
            "stop_times.txt", "calendar.txt",
        ):
            arquivo.writestr(nome, f"header\n{conteudo}\n")
        arquivo.writestr(
            "feed_info.txt",
            "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date\n"
            "SPTrans,https://www.sptrans.com.br,pt,20261118,20261231\n",
        )
    return buffer.getvalue()


class ClienteFalso:
    def __init__(self, conteudo):
        self.conteudo = conteudo

    def baixar_para(self, url, destino):
        assert url == "https://gtfs.exemplo/feed.zip"
        if isinstance(self.conteudo, Exception):
            destino.write(b"download parcial")
            raise self.conteudo
        destino.write(self.conteudo)


def test_mesmo_hash_retorna_mesma_versao_e_preserva_zip_original(app, tmp_path):
    original = _zip_gtfs()
    cliente = ClienteFalso(original)
    with app.app_context():
        primeira = capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=cliente)
        segunda = capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=cliente)

        assert primeira.id == segunda.id
        assert VersaoGTFS.query.count() == 1
        assert ArquivoArmazenado.query.count() == 1
        assert primeira.hash == hashlib.sha256(original).hexdigest()
        guardado = tmp_path / "instance" / "gtfs" / f"{primeira.hash}.zip"
        assert guardado.read_bytes() == original
        assert primeira.arquivo.chave == f"gtfs/{primeira.hash}.zip"


def test_arquivo_diferente_cria_nova_versao_sem_substituir_original(app, tmp_path):
    with app.app_context():
        primeira = capturar_gtfs(
            url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs("v1"))
        )
        segunda = capturar_gtfs(
            url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs("v2"))
        )

        assert primeira.id != segunda.id
        assert primeira.hash != segunda.hash
        assert VersaoGTFS.query.count() == 2
        assert (tmp_path / "instance" / "gtfs" / f"{primeira.hash}.zip").exists()
        assert (tmp_path / "instance" / "gtfs" / f"{segunda.hash}.zip").exists()


def test_download_com_falha_ou_zip_invalido_nao_cria_versao_nem_apaga_a_atual(app, tmp_path):
    with app.app_context():
        atual = capturar_gtfs(
            url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs())
        )
        com_falha = ClienteFalso(RuntimeError("conexão interrompida"))
        with pytest.raises(RuntimeError, match="interrompida"):
            capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=com_falha)
        with pytest.raises(GTFSValidationError):
            capturar_gtfs(
                url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(b"not a ZIP")
            )

        assert VersaoGTFS.query.count() == 1
        assert VersaoGTFS.query.one().id == atual.id
        assert (tmp_path / "instance" / "gtfs" / f"{atual.hash}.zip").read_bytes() == _zip_gtfs()
        assert not list((tmp_path / "instance" / "gtfs").glob("*.part"))


def test_hash_existente_recupera_zip_ausente_sem_mudar_captura(app, tmp_path):
    original = _zip_gtfs()
    captura = datetime(2026, 11, 18, 12, tzinfo=timezone(timedelta(hours=-3)))
    with app.app_context():
        primeira = capturar_gtfs(cliente=ClienteFalso(original),
                                 url="https://gtfs.exemplo/feed.zip", agora=captura)
        caminho = tmp_path / "instance" / "gtfs" / f"{primeira.hash}.zip"
        caminho.unlink()
        segunda = capturar_gtfs(cliente=ClienteFalso(original),
                                url="https://gtfs.exemplo/feed.zip")
        assert caminho.read_bytes() == original
        assert segunda.id == primeira.id
        assert segunda.capturada_em == captura.astimezone(timezone.utc)
        assert VersaoGTFS.query.count() == ArquivoArmazenado.query.count() == 1


def test_mesmo_banco_dois_armazenamentos_guardam_o_original(app, tmp_path):
    original = _zip_gtfs()
    with app.app_context():
        primeira = capturar_gtfs(url="https://gtfs.exemplo/feed.zip",
                                 cliente=ClienteFalso(original), diretorio=tmp_path / "pc1")
        segunda = capturar_gtfs(url="https://gtfs.exemplo/feed.zip",
                                cliente=ClienteFalso(original), diretorio=tmp_path / "pc2")
        assert segunda.id == primeira.id
        for nome in ("pc1", "pc2"):
            assert (tmp_path / nome / f"{primeira.hash}.zip").read_bytes() == original
        assert VersaoGTFS.query.count() == ArquivoArmazenado.query.count() == 1


def test_zip_existente_divergente_nao_e_sobrescrito_nem_retorna_sucesso(app, tmp_path):
    with app.app_context():
        primeira = capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs()))
        caminho = tmp_path / "instance" / "gtfs" / f"{primeira.hash}.zip"
        caminho.write_bytes(b"conteudo divergente")
        with pytest.raises(RuntimeError, match="divergente"):
            capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs()))
        assert caminho.read_bytes() == b"conteudo divergente"
        assert VersaoGTFS.query.count() == 1


def test_usa_diretorio_configurado_no_filestore(app, tmp_path):
    app.config['GTFS_STORAGE_DIR'] = str(tmp_path / 'volume')
    with app.app_context():
        versao = capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs()))
        assert (tmp_path / 'volume' / f'{versao.hash}.zip').read_bytes() == _zip_gtfs()
        assert not (tmp_path / 'instance' / 'gtfs').exists()


def test_falha_sql_preserva_objeto_e_permite_retomada(app, tmp_path, monkeypatch):
    def falhar():
        raise RuntimeError('falha SQL simulada')
    with app.app_context():
        with monkeypatch.context() as patch:
            patch.setattr(db.session, 'commit', falhar)
            with pytest.raises(RuntimeError, match='falha SQL'):
                capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs()))
        assert VersaoGTFS.query.count() == ArquivoArmazenado.query.count() == 0
        arquivos = list((tmp_path / 'instance' / 'gtfs').glob('*.zip'))
        assert len(arquivos) == 1
        versao = capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs()))
        assert arquivos[0].read_bytes() == _zip_gtfs()
        assert arquivos[0].stem == versao.hash
        assert VersaoGTFS.query.count() == ArquivoArmazenado.query.count() == 1
        assert not list(arquivos[0].parent.glob('*.part'))


@pytest.mark.parametrize('falha_primeira', [True, False])
def test_capturas_concorrentes_preservam_zip_e_uma_versao(tmp_path, monkeypatch, falha_primeira):
    app = create_app({'TESTING': True, 'GTFS_STORAGE_DIR': None, 'SQLALCHEMY_DATABASE_URI':
                      'sqlite:///' + (tmp_path / 'concorrencia.sqlite').as_posix()})
    app.instance_path = str(tmp_path / 'instance')
    with app.app_context():
        db.create_all()
    primeira_publicou = threading.Event()
    segunda_vai_confirmar = threading.Event()
    primeira_terminou = threading.Event()
    segunda_terminou = threading.Event()
    resultados, erros = {}, {}
    commit_real = db.session.commit

    def commit_controlado():
        if threading.current_thread().name == 'captura-A':
            primeira_publicou.set()
            assert segunda_vai_confirmar.wait(10)
            if falha_primeira:
                raise RuntimeError('falha antes do commit A')
            assert segunda_terminou.wait(10)
        else:
            segunda_vai_confirmar.set()
            if falha_primeira:
                assert primeira_terminou.wait(10)
        return commit_real()

    def executar(nome):
        try:
            with app.app_context():
                versao = capturar_gtfs(url="https://gtfs.exemplo/feed.zip", cliente=ClienteFalso(_zip_gtfs()))
                resultados[nome] = versao.id
        except Exception as erro:
            erros[nome] = erro
        finally:
            (primeira_terminou if nome == 'A' else segunda_terminou).set()

    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'commit', commit_controlado)
        a = threading.Thread(target=executar, args=('A',), name='captura-A')
        b = threading.Thread(target=executar, args=('B',), name='captura-B')
        a.start()
        assert primeira_publicou.wait(10)
        b.start()
        a.join(15)
        b.join(15)
        assert not a.is_alive() and not b.is_alive()
    try:
        if falha_primeira:
            assert set(erros) == {'A'}
            assert str(erros['A']) == 'falha antes do commit A'
            assert 'B' in resultados
        else:
            assert not erros
            assert resultados['A'] == resultados['B']
        with app.app_context():
            assert VersaoGTFS.query.count() == ArquivoArmazenado.query.count() == 1
            versao = VersaoGTFS.query.one()
            diretorio = tmp_path / 'instance' / 'gtfs'
            assert (diretorio / f'{versao.hash}.zip').read_bytes() == _zip_gtfs()
            assert not list(diretorio.glob('*.part'))
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
