"""Manifestos duráveis de planejamento US20 (``PLANO_US20``); não executa recálculos.

Cada item do plano guarda o resultado afetado e a ``regra`` alvo do recálculo.
Itens cujo tipo o worker não executa ficam com ``executavel = False`` e entram
apenas no registro (rastreabilidade), sem gerar pedidos fadados ao erro.
"""
import json
from hashlib import sha256

from sqlalchemy import func

from app import db
from app.models import ExecucaoProcessamento, RegistroBrutoGPS
from app.reprocessamento.execucao import REGRA_VIGENTE
from app.reprocessamento.regras import localizar_regra_e_dependentes
from app.reprocessamento.tardios import localizar_janelas_tardias


def _versoes(base, regra, codigo):
    for nome, valor in (("base", base), ("regra", regra), ("código", codigo)):
        if not isinstance(valor, str) or not valor.strip() or len(valor) > 128:
            raise ValueError(f"versão de {nome} inválida")


def _itens(resultados, regras):
    """``regras``: indicador -> regra alvo (None = sem executor)."""
    return [{"id": r.id, "indicador": r.indicador, "inicio": r.periodo_inicio.isoformat(),
             "fim": r.periodo_fim.isoformat(), "linha": r.linha_codigo,
             "sentido": r.sentido, "parada": r.parada_codigo, "trecho": r.trecho_id,
             "gtfs": r.versao_gtfs_id, "versao_calculo": r.versao_calculo,
             "versao_conjunto": r.versao_conjunto, "publicado_em": r.publicado_em.isoformat(),
             "regra": regras.get(r.indicador), "executavel": regras.get(r.indicador) is not None}
            for r in resultados]


def _registrar(*, origem, resultados, regras, base, regra, codigo, gtfs_id=None, detalhes=None,
               identidade=None):
    """Adiciona à transação corrente; o chamador deve confirmar com commit.

    ``identidade``: o que torna dois planos o mesmo pedido de detecção. Sem ela,
    o plano inteiro (com o snapshot dos brutos) identifica o plano.
    """
    _versoes(base, regra, codigo)
    if not resultados:
        raise ValueError("nenhum indicador publicado foi localizado")
    max_id = db.session.query(func.max(RegistroBrutoGPS.id)).scalar() or 0
    itens = _itens(resultados, regras)
    parametros = {"origem": origem, "resultados": itens, "detalhes": detalhes or {},
                  "snapshot_bruto_id_maximo": max_id,
                  "execucao_automatica_disponivel": False,
                  "sem_executor": [i["id"] for i in itens if not i["executavel"]]}
    identidade = identidade or {"parametros": parametros}
    identidade = {**identidade, "base": base, "regra": regra, "codigo": codigo}
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
    """US20-T01B: resultados de ``[inicio, fim)`` calculados com ``regra_anterior``.

    A regra nova precisa ser a implementada pelo código em execução para cada
    indicador afetado diretamente; senão o worker só produziria ERRO. Dependentes
    usam a regra vigente deles. Fora do intervalo e de outras regras: intocados.
    """
    if regra_anterior == versao_regra:
        raise ValueError("a regra nova deve ser diferente da anterior")
    resultados = localizar_regra_e_dependentes(regra_anterior, inicio, fim)
    diretos = {r.indicador for r in resultados if r.versao_calculo == regra_anterior}
    sem_suporte = sorted(i for i in diretos if REGRA_VIGENTE.get(i) != versao_regra)
    if sem_suporte:
        raise ValueError(f"regra {versao_regra} não implementada para {', '.join(sem_suporte)}")
    regras = {i: (versao_regra if i in diretos else REGRA_VIGENTE.get(i))
              for i in {r.indicador for r in resultados}}
    return _registrar(origem="REGRA", resultados=resultados, regras=regras, base=versao_base,
        regra=versao_regra, codigo=versao_codigo,
        detalhes={"regra_anterior": regra_anterior, "regra_nova": versao_regra,
                  "inicio": inicio.isoformat(), "fim": fim.isoformat(),
                  "indicadores_diretos": sorted(diretos),
                  "dependentes": sorted({r.indicador for r in resultados} - diretos),
                  "dependencias": "app.reprocessamento.dependencias (somente confirmadas)"})


def planejar_tardio(*, bruto_id, versao_base, versao_regra, versao_codigo, lacuna=None):
    """US20-T01A: plano para uma posição que chegou depois da publicação afetada.

    A mesma posição, com os mesmos resultados afetados e versões, gera o mesmo
    plano (o snapshot dos brutos fica registrado, mas fora da identidade).
    """
    bruto = db.session.get(RegistroBrutoGPS, bruto_id)
    if bruto is None or bruto.horario_gps is None or bruto.coletado_em is None:
        raise ValueError("bruto ou horários indisponíveis")
    impacto = localizar_janelas_tardias(bruto, lacuna=lacuna)
    if not impacto["tardio"]:
        raise ValueError("nenhum resultado afetado foi publicado antes da chegada; "
                         "o processamento regular cobre esta posição")
    resultados = impacto["resultados"] + impacto["sem_executor"]
    regras = {r.indicador: REGRA_VIGENTE.get(r.indicador) for r in resultados}
    iso = lambda v: v.isoformat()  # noqa: E731
    detalhes = {
        "bruto_id": bruto.id, "horario_evento": iso(impacto["horario_evento"]),
        "recebido_em": iso(impacto["recebido_em"]),
        "criterio_tardio": "resultado afetado publicado em ou antes do recebimento",
        "lacuna_passagem_seg": impacto["lacuna_passagem_seg"],
        "faixa_passagens": [iso(v) for v in impacto["faixa_passagens"]],
        "janelas": {k: [iso(v) for v in vs] for k, vs in impacto["janelas"].items()},
        "janelas_vizinhas": [iso(v) for v in impacto["janelas_vizinhas"]],
        "viagens": impacto["viagens"], "abrangencia_partidas": impacto["abrangencia_partidas"],
        "publicados_antes_da_chegada": impacto["publicados_antes_da_chegada"],
    }
    return _registrar(origem="GPS_TARDIO", resultados=resultados, regras=regras, base=versao_base,
        regra=versao_regra, codigo=versao_codigo, gtfs_id=impacto["versao_gtfs_id"],
        detalhes=detalhes, identidade={"origem": "GPS_TARDIO", "bruto_id": bruto.id,
                                       "resultados": sorted(r.id for r in resultados)})
