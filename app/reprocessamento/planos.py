"""Manifestos duráveis de planejamento US20; não executa recálculos."""
from hashlib import sha256
import json

from sqlalchemy import func

from app import db
from app.models import ExecucaoProcessamento, IndicadorOperacional, ObservacaoTratada, RegistroBrutoGPS
from app.reprocessamento.regras import localizar_regra_e_dependentes


def _versoes(base, regra, codigo):
    for nome, valor in (("base", base), ("regra", regra), ("código", codigo)):
        if not isinstance(valor, str) or not valor.strip() or len(valor) > 128:
            raise ValueError(f"versão de {nome} inválida")


def _registrar(*, origem, resultados, base, regra, codigo, gtfs_id=None, detalhes=None):
    """Adiciona à transação corrente; o chamador deve confirmar com commit."""
    _versoes(base, regra, codigo)
    if not resultados:
        raise ValueError("nenhum indicador publicado foi localizado")
    max_id = db.session.query(func.max(RegistroBrutoGPS.id)).scalar() or 0
    itens = [{"id": r.id, "indicador": r.indicador, "inicio": r.periodo_inicio.isoformat(),
              "fim": r.periodo_fim.isoformat(), "linha": r.linha_codigo,
              "sentido": r.sentido, "parada": r.parada_codigo, "trecho": r.trecho_id,
              "gtfs": r.versao_gtfs_id, "versao_calculo": r.versao_calculo,
              "versao_conjunto": r.versao_conjunto} for r in resultados]
    parametros = {"origem": origem, "resultados": itens, "detalhes": detalhes or {},
                  "snapshot_bruto_id_maximo": max_id,
                  "execucao_automatica_disponivel": False}
    identidade = {"parametros": parametros, "base": base, "regra": regra, "codigo": codigo}
    chave = "plano-us20:" + sha256(json.dumps(identidade, sort_keys=True, ensure_ascii=True,
                                                separators=(",", ":")).encode()).hexdigest()
    existente = ExecucaoProcessamento.query.filter_by(chave_logica=chave).one_or_none()
    if existente:
        return existente
    plano = ExecucaoProcessamento(tipo="PLANO_US20", chave_logica=chave, parametros=parametros,
        estado="AGUARDANDO", periodo_inicio=min(r.periodo_inicio for r in resultados),
        periodo_fim=max(r.periodo_fim for r in resultados), bruto_id_maximo=max_id,
        versao_base=base, versao_regra=regra, versao_codigo=codigo, versao_gtfs_id=gtfs_id)
    db.session.add(plano)
    db.session.flush()
    return plano


def planejar_regra(*, regra_anterior, inicio, fim, versao_base, versao_regra, versao_codigo):
    resultados = localizar_regra_e_dependentes(regra_anterior, inicio, fim)
    return _registrar(origem="REGRA", resultados=resultados, base=versao_base,
        regra=versao_regra, codigo=versao_codigo,
        detalhes={"regra_anterior": regra_anterior, "inicio": inicio.isoformat(),
                  "fim": fim.isoformat(), "dependencias_completas": False})


def planejar_tardio(*, bruto_id, versao_base, versao_regra, versao_codigo):
    """Usa toda a história publicada do escopo quando não há vínculo GPS/viagem.

    Detecta atraso pelo horário de publicação de um indicador da janela GPS.
    A abrangência é deliberadamente conservadora e pode incluir períodos alheios.
    """
    bruto = db.session.get(RegistroBrutoGPS, bruto_id)
    if bruto is None or bruto.horario_gps is None or bruto.coletado_em is None:
        raise ValueError("bruto ou horários indisponíveis")
    tratada = ObservacaoTratada.query.filter_by(bruto_id=bruto.id).one_or_none()
    if (tratada is None or tratada.valida is not True or not tratada.linha_codigo
            or not tratada.versao_gtfs_id or tratada.sentido_gtfs not in ("0", "1")):
        raise ValueError("é necessária correspondência válida com linha, sentido e GTFS")
    I = IndicadorOperacional
    escopo = I.query.filter_by(versao_gtfs_id=tratada.versao_gtfs_id,
                                linha_codigo=tratada.linha_codigo, sentido=tratada.sentido_gtfs)
    fechamento = escopo.filter(I.periodo_inicio <= bruto.horario_gps,
        I.periodo_fim > bruto.horario_gps, I.publicado_em <= bruto.coletado_em).first()
    if fechamento is None:
        raise ValueError("não há publicação anterior à chegada na janela do GPS")
    resultados = escopo.order_by(I.periodo_inicio, I.id).all()
    return _registrar(origem="GPS_TARDIO", resultados=resultados, base=versao_base,
        regra=versao_regra, codigo=versao_codigo, gtfs_id=tratada.versao_gtfs_id,
        detalhes={"bruto_id": bruto.id, "horario_gps": bruto.horario_gps.isoformat(),
                  "coletado_em": bruto.coletado_em.isoformat(),
                  "abrangencia": "HISTORICO_INTEGRAL_DO_ESCOPO",
                  "motivo": "ausente vínculo persistido entre observação GPS e viagem"})
