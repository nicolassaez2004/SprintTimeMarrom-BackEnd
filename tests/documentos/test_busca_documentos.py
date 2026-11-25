"""US05-T02: busca paginada e entrega do PDF original. Dados sintéticos."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.api import documentos as routes
from app.documentos.service import caminho_original, catalogar_documento
from app.models import ArquivoArmazenado, DocumentoOficial

AGORA = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


@pytest.fixture
def app(tmp_path):
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                            "MYSQL_SSL_CA": None, "DOCUMENTOS_STORAGE_DIR": None})
    aplicacao.instance_path = str(tmp_path / "instance")
    with aplicacao.app_context():
        event.listen(db.engine, "connect", lambda conexao, _: conexao.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def cliente(app):
    return app.test_client()


@pytest.fixture
def catalogar(app, tmp_path):
    def _catalogar(tipo, data_documento, conteudo=None):
        caminho = tmp_path / f"{tipo}-{data_documento}.pdf"
        caminho.write_bytes(conteudo or f"%PDF-1.4\n% sintetico {tipo} {data_documento}\n%%EOF\n".encode())
        documento, _ = catalogar_documento(
            caminho, titulo=f"Sintético {tipo} {data_documento}", tipo=tipo,
            data_documento=date.fromisoformat(data_documento), orgao="Órgão sintético",
            fonte="https://exemplo.invalid/doc.pdf", descoberto_em=AGORA - timedelta(days=1), agora=AGORA)
        return documento
    return _catalogar


@pytest.fixture
def acervo(catalogar):
    return {chave: catalogar(*chave) for chave in [
        ("CONTRATO", "2026-01-10"), ("CONTRATO", "2026-03-05"), ("CONTRATO", "2026-06-20"),
        ("INDICADOR_OFICIAL", "2026-03-15"), ("INDICADOR_OFICIAL", "2026-07-01"),
    ]}


def _datas(resposta):
    return [(item["tipo"], item["data_documento"]) for item in resposta.get_json()["itens"]]


def test_filtros_combinados_por_tipo_e_intervalo_de_datas(cliente, acervo):
    resposta = cliente.get("/documentos?tipo=CONTRATO&data_inicio=2026-03-01&data_fim=2026-06-30")
    assert resposta.status_code == 200
    assert _datas(resposta) == [("CONTRATO", "2026-06-20"), ("CONTRATO", "2026-03-05")]
    corpo = resposta.get_json()
    assert corpo["filtros"] == {"tipo": "CONTRATO", "data_inicio": "2026-03-01", "data_fim": "2026-06-30"}
    assert corpo["paginacao"]["total"] == 2
    # Limites inclusivos e filtros isolados.
    assert _datas(cliente.get("/documentos?data_inicio=2026-03-15&data_fim=2026-03-15")) == [
        ("INDICADOR_OFICIAL", "2026-03-15")]
    assert len(_datas(cliente.get("/api/documentos?tipo=INDICADOR_OFICIAL"))) == 2
    assert len(_datas(cliente.get("/documentos?data_fim=2026-03-05"))) == 2


def test_item_preserva_procedencia_e_metadados(cliente, acervo):
    item = cliente.get("/documentos?tipo=CONTRATO&data_inicio=2026-01-10&data_fim=2026-01-10").get_json()["itens"][0]
    documento = acervo[("CONTRATO", "2026-01-10")]
    assert item["id"] == documento.id and item["fonte"] == "https://exemplo.invalid/doc.pdf"
    assert item["orgao"] == "Órgão sintético" and item["prazo"]["cumprido"] is True
    assert item["arquivo"]["sha256"] == documento.arquivo.sha256


def test_paginacao_ordenada_sem_repeticao(cliente, acervo):
    paginas = [cliente.get(f"/documentos?por_pagina=2&pagina={n}").get_json() for n in (1, 2, 3)]
    assert [p["paginacao"] for p in paginas][0] == {"pagina": 1, "por_pagina": 2, "total": 5, "paginas": 3}
    assert [len(p["itens"]) for p in paginas] == [2, 2, 1]
    ids = [item["id"] for p in paginas for item in p["itens"]]
    assert len(set(ids)) == 5
    datas = [item["data_documento"] for p in paginas for item in p["itens"]]
    assert datas == sorted(datas, reverse=True)
    padrao = cliente.get("/documentos").get_json()["paginacao"]
    assert (padrao["pagina"], padrao["por_pagina"]) == (1, 20)


def test_consulta_vazia_e_pagina_alem_do_fim(cliente, acervo):
    vazia = cliente.get("/documentos?tipo=INEXISTENTE")
    assert vazia.status_code == 200
    assert vazia.get_json()["itens"] == [] and vazia.get_json()["paginacao"]["total"] == 0
    alem = cliente.get("/documentos?pagina=9").get_json()
    assert alem["itens"] == [] and alem["paginacao"]["total"] == 5


def test_acervo_sem_documentos(cliente):
    corpo = cliente.get("/documentos").get_json()
    assert corpo["itens"] == [] and corpo["paginacao"] == {"pagina": 1, "por_pagina": 20, "total": 0, "paginas": 0}


@pytest.mark.parametrize("consulta, campo", [
    ("data_inicio=2026-13-01", "data_inicio"),
    ("data_inicio=01/03/2026", "data_inicio"),
    ("data_fim=2026-02-30", "data_fim"),
    ("data_inicio=2026-05-01&data_fim=2026-04-30", "data_fim"),
    ("pagina=0", "pagina"),
    ("pagina=-1", "pagina"),
    ("pagina=abc", "pagina"),
    ("por_pagina=101", "por_pagina"),
    ("por_pagina=1.5", "por_pagina"),
    ("tipo=", "tipo"),
    ("tipo=" + "X" * 65, "tipo"),
    ("tipo=A&tipo=B", "tipo"),
    ("orgao=SPTrans", "orgao"),
])
def test_parametros_invalidos(cliente, consulta, campo):
    resposta = cliente.get(f"/documentos?{consulta}")
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["codigo"] == "PARAMETRO_INVALIDO"
    assert resposta.get_json()["erro"]["campo"] == campo


def test_entrega_pdf_original_identico(cliente, catalogar):
    conteudo = b"%PDF-1.7\n% original sintetico\n%%EOF\n"
    documento = catalogar("CONTRATO", "2026-02-01", conteudo)
    for prefixo in ("", "/api"):
        resposta = cliente.get(f"{prefixo}/documentos/{documento.id}/arquivo")
        assert resposta.status_code == 200
        assert resposta.data == conteudo
        assert resposta.mimetype == "application/pdf"
        assert "CONTRATO-2026-02-01.pdf" in resposta.headers["Content-Disposition"]
        assert resposta.headers["X-Conteudo-SHA256"] == documento.arquivo.sha256
        assert resposta.headers["X-Content-Type-Options"] == "nosniff"
        resposta.close()


def test_documento_inexistente_retorna_404(cliente):
    resposta = cliente.get("/documentos/nao-existe/arquivo")
    assert resposta.status_code == 404
    assert resposta.get_json()["erro"]["codigo"] == "DOCUMENTO_NAO_ENCONTRADO"


def test_pdf_ausente_ou_adulterado_retorna_503(cliente, catalogar):
    documento = catalogar("CONTRATO", "2026-02-01")
    caminho = caminho_original(documento.id)
    caminho.write_bytes(b"%PDF-1.4 adulterado")
    resposta = cliente.get(f"/documentos/{documento.id}/arquivo")
    assert resposta.status_code == 503
    assert resposta.get_json()["erro"]["codigo"] == "ARQUIVO_INDISPONIVEL"
    caminho.unlink()
    assert cliente.get(f"/documentos/{documento.id}/arquivo").status_code == 503


def test_so_entrega_pdf_catalogado(cliente, app, catalogar):
    # Um ArquivoArmazenado não catalogado (ex.: ZIP GTFS) não tem rota própria.
    gtfs = ArquivoArmazenado(chave="gtfs/x.zip", nome_original="x.zip", mime_type="application/zip",
                             tamanho_bytes=1, sha256="0" * 64)
    db.session.add(gtfs)
    db.session.commit()
    assert cliente.get(f"/documentos/{gtfs.id}/arquivo").status_code == 404
    # Um documento vinculado por fora da rotina a um arquivo não-PDF também não é entregue.
    db.session.add(DocumentoOficial(id="vinculo-manual", titulo="t", tipo="t", data_documento=date(2026, 1, 1),
                                    orgao="o", fonte="f", descoberto_em=AGORA, catalogado_em=AGORA,
                                    arquivo_id=gtfs.id))
    db.session.commit()
    resposta = cliente.get("/documentos/vinculo-manual/arquivo")
    assert resposta.status_code == 503


def test_sem_endpoints_de_escrita(cliente, catalogar):
    documento = catalogar("CONTRATO", "2026-02-01")
    assert cliente.post("/documentos", json={}).status_code == 405
    for metodo in (cliente.put, cliente.patch, cliente.delete):
        assert metodo(f"/documentos/{documento.id}/arquivo").status_code == 405
    assert cliente.get(f"/documentos/{documento.id}").status_code == 404


def test_falha_de_banco_retorna_503_sem_detalhes(cliente, monkeypatch):
    def falhar(**_):
        raise OperationalError("SELECT ...", {}, Exception("senha=segredo"))
    monkeypatch.setattr(routes, "buscar_documentos", falhar)
    resposta = cliente.get("/documentos")
    assert resposta.status_code == 503
    assert "segredo" not in resposta.get_data(as_text=True)
