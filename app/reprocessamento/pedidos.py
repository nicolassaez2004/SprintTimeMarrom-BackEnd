"""US20-T02A: pedidos duráveis de recálculo, registrados pelo ``JobService``.

Recebe as janelas localizadas pela US20-T01B (``planos``/``regras``/``tardios``)
e pede ao ``JobService`` um ``ExecucaoProcessamento`` por indicador, escopo e
janela. Não executa recálculos nem cria fila: o worker reserva os pedidos pelo
protocolo existente (estado, ``tentativas``, ``reserva_*``).
"""
from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import func

from app import db
from app.jobs import JobService
from app.models import IndicadorOperacional, RegistroBrutoGPS

TIPO = "RECALCULO_INDICADOR"
ESCOPO = ("gtfs", "linha", "sentido", "parada", "trecho")


def _instante(valor):
    instante = datetime.fromisoformat(valor) if isinstance(valor, str) else valor
    if instante is None or instante.utcoffset() is None:
        raise ValueError("a janela deve ter fuso horário")
    return instante.astimezone(timezone.utc)


def _item(resultado):
    """Aceita o item de ``plano.parametros['resultados']`` ou o próprio indicador."""
    if isinstance(resultado, IndicadorOperacional):
        r = resultado
        resultado = {"id": r.id, "indicador": r.indicador, "inicio": r.periodo_inicio,
                     "fim": r.periodo_fim, "gtfs": r.versao_gtfs_id, "linha": r.linha_codigo,
                     "sentido": r.sentido, "parada": r.parada_codigo, "trecho": r.trecho_id}
    inicio, fim = _instante(resultado["inicio"]), _instante(resultado["fim"])
    if fim <= inicio or not resultado.get("indicador"):
        raise ValueError("indicador e janela [início, fim) são obrigatórios")
    return {"id": resultado.get("id"), "indicador": resultado["indicador"], "inicio": inicio,
            "fim": fim, "escopo": {campo: resultado.get(campo) for campo in ESCOPO}}


def chave_logica(item, versao_base, versao_regra):
    """Indicador + escopo + janela + versões dos dados e da regra.

    ``versao_codigo`` e o snapshot dos brutos ficam registrados, mas fora da
    chave: repetir o pedido após novo deploy ou novo GPS não cria outro trabalho.
    Uma nova versão dos dados deve chegar como outra ``versao_base``.
    """
    identidade = {"tipo": TIPO, "indicador": item["indicador"], "escopo": item["escopo"],
                  "inicio": item["inicio"].isoformat(), "fim": item["fim"].isoformat(),
                  "versao_base": versao_base, "versao_regra": versao_regra}
    texto = json.dumps(identidade, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "recalculo-us20:" + sha256(texto.encode()).hexdigest()


def registrar_pedidos(resultados, *, versao_base, versao_regra, versao_codigo,
                      origem, bruto_id_maximo=None):
    """Pede um recálculo por janela afetada, numa transação; retorna os pedidos.

    Resultados antigos da mesma janela e escopo viram um único pedido.
    Idempotência e concorrência: ver ``JobService.solicitar``.
    """
    for nome, valor in (("base", versao_base), ("regra", versao_regra), ("código", versao_codigo)):
        if not isinstance(valor, str) or not valor.strip() or len(valor) > 128:
            raise ValueError(f"versão de {nome} inválida")
    grupos = {}
    for item in map(_item, resultados):
        grupo = grupos.setdefault(chave_logica(item, versao_base, versao_regra), (item, []))
        if item["id"] is not None:
            grupo[1].append(item["id"])
    if not grupos:
        raise ValueError("nenhuma janela afetada foi informada")
    if bruto_id_maximo is None:
        bruto_id_maximo = db.session.query(func.max(RegistroBrutoGPS.id)).scalar() or 0
    return JobService().solicitar([dict(
        tipo=TIPO, chave_logica=chave, periodo_inicio=item["inicio"], periodo_fim=item["fim"],
        versao_gtfs_id=item["escopo"]["gtfs"], bruto_id_maximo=bruto_id_maximo,
        versao_base=versao_base, versao_regra=versao_regra, versao_codigo=versao_codigo,
        parametros={"indicador": item["indicador"], "escopo": item["escopo"],
                    "janela": {"inicio": item["inicio"].isoformat(),
                               "fim_exclusivo": item["fim"].isoformat()},
                    "resultados_ids": sorted(ids), "origem": origem,
                    "snapshot_bruto_id_maximo": bruto_id_maximo},
    ) for chave, (item, ids) in grupos.items()])


def registrar_pedidos_do_plano(plano):
    """Converte o manifesto ``PLANO_US20`` da T01B em pedidos, preservando a origem.

    O plano é confirmado antes: ele é o registro durável da detecção e não deve
    ser desfeito por uma falha ou repetição na gravação dos pedidos.
    """
    db.session.commit()
    parametros = plano.parametros
    return registrar_pedidos(
        parametros["resultados"], versao_base=plano.versao_base, versao_regra=plano.versao_regra,
        versao_codigo=plano.versao_codigo, bruto_id_maximo=plano.bruto_id_maximo,
        origem={"plano_id": plano.id, "tipo": parametros.get("origem"),
                "detalhes": parametros.get("detalhes", {})})


def pedidos_pendentes(agora=None):
    """Pedidos de recálculo a retomar após reinício."""
    return JobService().pendentes(TIPO, agora)
