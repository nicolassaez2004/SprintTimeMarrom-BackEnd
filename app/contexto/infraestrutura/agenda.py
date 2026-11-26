"""Execução mensal, rastreável e idempotente da atualização de infraestrutura."""
from datetime import timezone

from app import db
from app.contexto.infraestrutura.associacao import classificar_rede_vigente
from app.contexto.infraestrutura.importacao import importar_geojson
from app.core.tempo import FUSO
from app.models import AtualizacaoMensalInfraestrutura


def _mes(agora):
    """Mês civil de São Paulo (fuso operacional do projeto), não o de UTC."""
    if agora.tzinfo is None:
        raise ValueError("a agenda mensal precisa de instante com fuso")
    return agora.astimezone(FUSO).strftime("%Y-%m")


def registrar_classificacao_vigente(agora):
    """Depois da atualização OK do mês, classifica a rede GTFS vigente uma vez por
    (rede, conjunto de snapshots) e anota o resumo no recibo mensal."""
    recibo = db.session.get(AtualizacaoMensalInfraestrutura, _mes(agora))
    if recibo is None or recibo.estado != "OK":
        return None
    anteriores = recibo.resultado.get("classificacoes", [])
    feitos = [(c.get("versao_gtfs_id"), c.get("versao_conjunto")) for c in anteriores]
    resumo = classificar_rede_vigente(agora, feitos)
    if resumo is None or "versao_conjunto" not in resumo:
        return resumo  # já feito, ou motivo (sem rede/snapshots): tenta de novo depois
    recibo = db.session.get(AtualizacaoMensalInfraestrutura, recibo.mes)
    recibo.resultado = {**recibo.resultado, "classificacoes": [*anteriores, resumo]}
    db.session.commit()
    return resumo


def atualizar_mensal(agora, carregar_bases):
    """Atualiza as fontes uma vez por mês.

    ``carregar_bases(agora)`` deve devolver itens de entrada aceitos por
    ``importar_geojson``; fica injetável para testes e para trocar a captura
    HTTP sem acoplar persistência ao fornecedor.
    """
    mes = _mes(agora)
    agora = agora.astimezone(timezone.utc)
    recibo = db.session.get(AtualizacaoMensalInfraestrutura, mes)
    if recibo and recibo.estado == "OK":
        return recibo.resultado
    if recibo is None:
        recibo = AtualizacaoMensalInfraestrutura(mes=mes, iniciada_em=agora,
                                                 estado="EM_ANDAMENTO", resultado={})
        db.session.add(recibo)
    else:
        recibo.iniciada_em = agora
        recibo.finalizada_em = None
        recibo.estado = "EM_ANDAMENTO"
    db.session.commit()
    try:
        versoes = []
        for entrada in carregar_bases(agora):
            versao = importar_geojson(**entrada)
            versoes.append({"versao_id": versao.id, "fonte": versao.fonte,
                            "sha256": versao.sha256,
                            "resumo_mudancas": versao.metadados.get("resumo_mudancas", {})})
        resultado = {"mes": mes, "versoes": versoes}
        recibo = db.session.get(AtualizacaoMensalInfraestrutura, mes)
        recibo.estado = "OK"
        recibo.finalizada_em = agora
        recibo.resultado = resultado
        db.session.commit()
        return resultado
    except Exception as erro:
        db.session.rollback()
        recibo = db.session.get(AtualizacaoMensalInfraestrutura, mes)
        recibo.estado = "FALHA"
        recibo.finalizada_em = agora
        recibo.resultado = {"mes": mes, "tipo_erro": type(erro).__name__}
        db.session.commit()
        raise
