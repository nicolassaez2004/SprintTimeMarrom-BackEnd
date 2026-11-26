"""Consulta snapshots de infraestrutura vigentes em uma data."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.models import ClassificacaoInfraestrutura, VersaoInfraestrutura

FUSO_MUNICIPIO = ZoneInfo("America/Sao_Paulo")


def versao_em(fonte, categoria, quando):
    """Retorna a última versão capturada até o instante/data consultado."""
    if isinstance(quando, date) and not isinstance(quando, datetime):
        quando = datetime.combine(quando, time.max, tzinfo=FUSO_MUNICIPIO).astimezone(timezone.utc)
    elif quando.tzinfo is None:
        raise ValueError("a consulta precisa de data ou instante com fuso")
    else:
        quando = quando.astimezone(timezone.utc)
    candidatas = VersaoInfraestrutura.query.filter(
        VersaoInfraestrutura.fonte == fonte,
        VersaoInfraestrutura.capturada_em <= quando,
    ).order_by(VersaoInfraestrutura.capturada_em.desc(), VersaoInfraestrutura.id.desc()).all()
    return next((v for v in candidatas if v.metadados.get("categoria") == categoria), None)


def consultar_em(fonte, categoria, quando):
    """Retorna versão e classificações; a geometria antiga nunca é substituída."""
    versao = versao_em(fonte, categoria, quando)
    if versao is None:
        return {"versao": None, "classificacoes": []}
    feicoes = ClassificacaoInfraestrutura.query.filter_by(versao_id=versao.id).order_by(
        ClassificacaoInfraestrutura.identificador_fonte,
    ).all()
    return {"versao": versao, "classificacoes": feicoes}
