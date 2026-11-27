"""US20-T02A: pedidos duráveis de recálculo, registrados pelo ``JobService``.

Recebe as janelas localizadas pela US20-T01B (``planos``/``regras``/``tardios``)
e pede ao ``JobService`` um ``ExecucaoProcessamento`` por indicador, escopo e
janela. Não executa recálculos nem cria fila: o worker reserva os pedidos pelo
protocolo existente (estado, ``tentativas``, ``reserva_*``).
"""
import json
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import func, or_

from app import db
from app.jobs import JobService
from app.models import ExecucaoProcessamento, IndicadorOperacional, RegistroBrutoGPS

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
            "fim": fim, "escopo": {campo: resultado.get(campo) for campo in ESCOPO},
            "regra": resultado.get("regra")}


def chave_logica(item, versao_base, versao_regra):
    """Indicador + escopo + janela + versões dos dados e da regra.

    É a identidade-base. ``versao_codigo`` fica registrado, mas fora dela: novo
    deploy não cria outro trabalho. O snapshot dos brutos decide a geração
    (``chave_da_geracao``): dados novos depois de o pedido já ter lido os
    anteriores geram ``base:<snapshot>``, nunca são descartados.
    """
    identidade = {"tipo": TIPO, "indicador": item["indicador"], "escopo": item["escopo"],
                  "inicio": item["inicio"].isoformat(), "fim": item["fim"].isoformat(),
                  "versao_base": versao_base, "versao_regra": versao_regra}
    texto = json.dumps(identidade, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "recalculo-us20:" + sha256(texto.encode()).hexdigest()


def chave_da_geracao(base, bruto_id_maximo, limite=None):
    """Chave a usar para um pedido cuja identidade é ``base``.

    Reutiliza o pedido que ainda vai ler os dados (``AGUARDANDO`` ou ``ERRO``
    com tentativas restantes) ou que já leu o mesmo estado (snapshot dos brutos
    igual ou maior): repetição não cria trabalho. Se todos os pedidos da base já
    leram um estado anterior (``PROCESSANDO``, ``PRONTO`` ou ``ERRO`` esgotado),
    os dados novos abrem uma geração ``base:<snapshot>``; um pedido concluído não
    faz uma atualização legítima desaparecer. Pedidos anteriores ficam intactos.
    """
    limite = JobService.LIMITE_TENTATIVAS if limite is None else limite
    E = ExecucaoProcessamento
    jobs = (E.query.filter(or_(E.chave_logica == base, E.chave_logica.like(base + ":%")))
            .order_by(E.criada_em.desc(), E.id.desc()).all())
    if not jobs:
        return base
    for job in jobs:
        if job.estado == "AGUARDANDO" or (job.estado == "ERRO" and job.tentativas < limite):
            return job.chave_logica
    if max(j.bruto_id_maximo for j in jobs) >= bruto_id_maximo:
        return jobs[0].chave_logica
    return f"{base}:{bruto_id_maximo}"


def registrar_pedidos(resultados, *, versao_base, versao_regra, versao_codigo,
                      origem, bruto_id_maximo=None):
    """Pede um recálculo por janela afetada, numa transação; retorna os pedidos.

    Resultados antigos da mesma janela e escopo viram um único pedido. Cada item
    pode trazer sua ``regra`` alvo (planos com indicadores de regras distintas);
    sem ela vale ``versao_regra``. Idempotência e concorrência: ver
    ``JobService.solicitar`` e ``chave_da_geracao``.
    """
    for nome, valor in (("base", versao_base), ("regra", versao_regra), ("código", versao_codigo)):
        if not isinstance(valor, str) or not valor.strip() or len(valor) > 128:
            raise ValueError(f"versão de {nome} inválida")
    grupos = {}
    for item in map(_item, resultados):
        regra = item["regra"] or versao_regra
        if not isinstance(regra, str) or not regra.strip() or len(regra) > 128:
            raise ValueError("versão de regra inválida")
        grupo = grupos.setdefault(chave_logica(item, versao_base, regra), (item, regra, []))
        if item["id"] is not None:
            grupo[2].append(item["id"])
    if not grupos:
        raise ValueError("nenhuma janela afetada foi informada")
    if bruto_id_maximo is None:
        bruto_id_maximo = db.session.query(func.max(RegistroBrutoGPS.id)).scalar() or 0
    return JobService().solicitar([dict(
        tipo=TIPO, chave_logica=chave_da_geracao(base, bruto_id_maximo),
        periodo_inicio=item["inicio"], periodo_fim=item["fim"],
        versao_gtfs_id=item["escopo"]["gtfs"], bruto_id_maximo=bruto_id_maximo,
        versao_base=versao_base, versao_regra=regra, versao_codigo=versao_codigo,
        parametros={"indicador": item["indicador"], "escopo": item["escopo"],
                    "janela": {"inicio": item["inicio"].isoformat(),
                               "fim_exclusivo": item["fim"].isoformat()},
                    "resultados_ids": sorted(ids), "origem": origem, "chave_base": base,
                    "snapshot_bruto_id_maximo": bruto_id_maximo},
    ) for base, (item, regra, ids) in grupos.items()])


def registrar_pedidos_do_plano(plano):
    """Converte o manifesto ``PLANO_US20`` da T01B em pedidos, preservando a origem.

    O plano é confirmado antes: ele é o registro durável da detecção e não deve
    ser desfeito por uma falha ou repetição na gravação dos pedidos.
    """
    db.session.commit()
    parametros = plano.parametros
    itens = [i for i in parametros["resultados"] if i.get("executavel", True)]
    if not itens:  # só tipos sem executor: o plano registra, nenhum pedido é criado
        return []
    return registrar_pedidos(
        itens, versao_base=plano.versao_base, versao_regra=plano.versao_regra,
        versao_codigo=plano.versao_codigo, bruto_id_maximo=plano.bruto_id_maximo,
        origem={"plano_id": plano.id, "tipo": parametros.get("origem"),
                "detalhes": parametros.get("detalhes", {})})


def pedidos_pendentes(agora=None):
    """Pedidos de recálculo a retomar após reinício."""
    return JobService().pendentes(TIPO, agora)
