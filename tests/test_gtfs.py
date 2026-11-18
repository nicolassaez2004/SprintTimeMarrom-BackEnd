import hashlib
import io
import zipfile

import pytest

from app import create_app, db
from app.gtfs.service import GTFSValidationError, capturar_gtfs
from app.models import ArquivoArmazenado, VersaoGTFS


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
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
