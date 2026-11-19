"""Última posição conhecida de cada veículo (US22-T01), lida só do banco.

Nunca chama a SPTrans: lê `RegistroBrutoGPS` já gravado pelo coletor. A
última posição de um veículo é a do registro coletado mais recentemente que
tenha coordenadas e horário GPS; o filtro de linha é aplicado **depois** dessa
escolha, para um ônibus que trocou de linha não aparecer na linha antiga.
"""

from datetime import timedelta

from sqlalchemy import and_, func

from app import db
from app.models import (
    ExecucaoColeta,
    Linha,
    ObservacaoTratada,
    RegistroBrutoGPS,
    agora_utc,
)

# Critério da US22: no máximo 60 s de defasagem em relação à coleta.
LIMITE_RECENTE_SEGUNDOS = 60
# Veículos sem posição nesta janela não aparecem no mapa ao vivo.
JANELA_CONSULTA = timedelta(hours=24)


def _iso(valor):
    return valor.isoformat() if valor else None


def _linha_gtfs(observacao, linha):
    if observacao is None or observacao.linha_codigo is None:
        return None
    return {
        "codigo": observacao.linha_codigo,
        "nome": linha.nome if linha else None,
        "sentido": observacao.sentido_gtfs,
        "versao_gtfs_id": observacao.versao_gtfs_id,
    }


def ultimas_posicoes(*, linha=None, linha_gtfs=None, agora=None):
    """`linha` = código Olho Vivo (`cl`); `linha_gtfs` = `route_id` ligado na US03."""
    agora = agora or agora_utc()
    desde = agora - JANELA_CONSULTA
    R = RegistroBrutoGPS
    com_posicao = (
        R.veiculo_prefixo.is_not(None), R.latitude.is_not(None),
        R.longitude.is_not(None), R.horario_gps.is_not(None),
        R.coletado_em >= desde, R.coletado_em <= agora,
    )
    ultimos = (
        db.session.query(R.veiculo_prefixo.label("prefixo"),
                         func.max(R.coletado_em).label("coletado_em"))
        .filter(*com_posicao).group_by(R.veiculo_prefixo).subquery()
    )
    consulta = (
        db.session.query(R, ObservacaoTratada, Linha)
        .join(ultimos, and_(R.veiculo_prefixo == ultimos.c.prefixo,
                            R.coletado_em == ultimos.c.coletado_em))
        .filter(*com_posicao)
        .outerjoin(ObservacaoTratada, ObservacaoTratada.bruto_id == R.id)
        .outerjoin(Linha, and_(Linha.versao_gtfs_id == ObservacaoTratada.versao_gtfs_id,
                               Linha.codigo == ObservacaoTratada.linha_codigo))
        .order_by(R.veiculo_prefixo, R.horario_gps.desc(), R.id.desc())
    )
    # Mesmo ciclo com duas posições do veículo: fica a de horário GPS mais novo.
    escolhidos = {}
    for registro, observacao, linha_rede in consulta:
        escolhidos.setdefault(registro.veiculo_prefixo, (registro, observacao, linha_rede))

    veiculos = []
    for registro, observacao, linha_rede in escolhidos.values():
        gtfs = _linha_gtfs(observacao, linha_rede)
        if linha is not None and registro.linha_origem != linha:
            continue
        if linha_gtfs is not None and (gtfs is None or gtfs["codigo"] != linha_gtfs):
            continue
        idade = (agora - registro.coletado_em).total_seconds()
        veiculos.append({
            "prefixo": registro.veiculo_prefixo,
            "linha": {
                "codigo_olho_vivo": registro.linha_origem,
                "sentido_olho_vivo": registro.sentido_origem,
                "gtfs": gtfs,
            },
            "latitude": registro.latitude,
            "longitude": registro.longitude,
            "horario_gps": _iso(registro.horario_gps),
            "coletado_em": _iso(registro.coletado_em),
            "idade_segundos": round(idade, 3),
            "recente": idade <= LIMITE_RECENTE_SEGUNDOS,
        })

    ultima_coleta = (
        db.session.query(func.max(ExecucaoColeta.iniciada_em))
        .filter(ExecucaoColeta.estado == "OK").scalar()
    )
    return {
        "gerado_em": _iso(agora),
        "filtros": {"linha": linha, "linha_gtfs": linha_gtfs},
        "limite_recente_segundos": LIMITE_RECENTE_SEGUNDOS,
        "janela_consulta_inicio": _iso(desde),
        "ultima_coleta_ok_em": _iso(ultima_coleta),
        "total": len(veiculos),
        "veiculos": veiculos,
    }
