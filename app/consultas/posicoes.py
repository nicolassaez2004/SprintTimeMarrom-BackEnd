"""Última posição conhecida de cada veículo (US22-T01), lida só do banco.

Nunca chama a SPTrans: lê `RegistroBrutoGPS` já gravado pelo coletor. A
última posição de um veículo é a do registro coletado mais recentemente que
tenha coordenadas e horário GPS; o filtro de linha é aplicado **depois** dessa
escolha, para um ônibus que trocou de linha não aparecer na linha antiga.
"""

from datetime import timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased

from app import db
from app.models import (
    ExecucaoColeta,
    Linha,
    ObservacaoTratada,
    RegistroBrutoGPS,
    Veiculo,
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


def versao_posicoes():
    """Último ciclo OK: os brutos só mudam quando um ciclo é concluído (ETag)."""
    return (
        db.session.query(func.max(ExecucaoColeta.id))
        .filter(ExecucaoColeta.estado == "OK").scalar() or 0
    )


def ultimas_posicoes(*, linha=None, linha_gtfs=None, agora=None):
    """`linha` = código Olho Vivo (`cl`); `linha_gtfs` = `route_id` ligado na US03."""
    agora = agora or agora_utc()
    desde = agora - JANELA_CONSULTA
    R = RegistroBrutoGPS
    ultima = aliased(RegistroBrutoGPS)
    # Uma busca por veículo no índice (prefixo_origem, coletado_em), do mais
    # novo para o mais antigo: não agrupa as 24 h de brutos (US22/US27). O
    # coletor grava prefixo_origem = veiculo_prefixo (FK para veiculos).
    ultimo_id = (
        select(ultima.id)
        .where(ultima.prefixo_origem == Veiculo.prefixo,
               ultima.veiculo_prefixo.is_not(None), ultima.latitude.is_not(None),
               ultima.longitude.is_not(None), ultima.horario_gps.is_not(None),
               ultima.coletado_em >= desde, ultima.coletado_em <= agora)
        # Só colunas do índice (o InnoDB anexa o id): sem filesort por veículo.
        .order_by(ultima.coletado_em.desc(), ultima.id.desc())
        .limit(1).correlate(Veiculo).scalar_subquery()
    )
    consulta = (
        db.session.query(R, ObservacaoTratada, Linha)
        .select_from(Veiculo)
        .join(R, R.id == ultimo_id)
        .outerjoin(ObservacaoTratada, ObservacaoTratada.bruto_id == R.id)
        .outerjoin(Linha, and_(Linha.versao_gtfs_id == ObservacaoTratada.versao_gtfs_id,
                               Linha.codigo == ObservacaoTratada.linha_codigo))
        .order_by(R.veiculo_prefixo)
    )
    # A subconsulta já escolhe um registro por veículo (empate no ciclo: maior id).
    veiculos = []
    for registro, observacao, linha_rede in consulta:
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
