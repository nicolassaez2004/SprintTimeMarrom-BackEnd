"""Exportação CSV dos indicadores publicados pela API (US10)."""

import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import and_, not_, or_

from app import db
from app.indicadores.agenda import TIPOS_PUBLICACAO, ordem_publicacao
from app.indicadores.series import _janelas_horarias, _ou_balanceado
from app.indicadores.service import filtrar_periodo, consulta_base, serializar
from app.models import ExecucaoProcessamento, IndicadorOperacional, Linha
from app.operacao.arquivos import gravar_json
from app.exportacoes.parquet import diretorio_exportacoes, periodo_utc

FONTE = ("Observa SP; indicadores calculados a partir de dados públicos da SPTrans "
         "(API Olho Vivo e GTFS/programação oficial)")
FONTE_URL = "https://www.sptrans.com.br/desenvolvedores/"
# US10: "mesmo prazo e licença da US09" (requisito), a mesma do Parquet dos brutos.
LICENCA = "CC BY 4.0"
LICENCA_URL = "https://creativecommons.org/licenses/by/4.0/"
# Condições de uso da fonte, preservadas junto à licença (confirmação jurídica: pendência do PO).
CONDICOES_FONTE = ("Condições de acesso da SPTrans: acesso gratuito; dados não podem ser alugados, "
                   "sublicenciados ou vendidos.")
CONDICOES_FONTE_URL = (
    "https://www.sptrans.com.br/desenvolvedores/cadastro-desenvolvedores/"
    "registro-desenvolvedores-pj"
)

# Fonte e licença também em cada linha: o arquivo baixado as indica por si só (US09/US10).
CAMPOS = (
    "linha", "nome_linha", "sentido", "tipo", "valor", "unidade", "inicio", "fim",
    "parada", "trecho", "versao_gtfs_id", "versao_conjunto",
    "versao_correspondencia", "versao_calculo", "contagem_numerador",
    "contagem_denominador", "qualidade", "fonte", "licenca",
)


def _serializar_linhas(registros):
    pares = {(r.versao_gtfs_id, r.linha_codigo) for r in registros}
    nomes = {}
    if pares:
        condicoes = [and_(Linha.versao_gtfs_id == versao, Linha.codigo == codigo)
                     for versao, codigo in pares]
        nomes = {(versao, codigo): nome for versao, codigo, nome in db.session.query(
            Linha.versao_gtfs_id, Linha.codigo, Linha.nome).filter(or_(*condicoes)).all()}
    buffer = io.StringIO(newline="")
    escritor = csv.DictWriter(
        buffer, fieldnames=CAMPOS, extrasaction="ignore", lineterminator="\r\n",
    )
    escritor.writeheader()
    for modelo in registros:
        registro = serializar(modelo)
        linha = {
            **registro,
            "nome_linha": nomes.get((registro["versao_gtfs_id"], registro["linha"]), ""),
            "contagem_numerador": registro["contagens"]["numerador"],
            "contagem_denominador": registro["contagens"]["denominador"],
            "qualidade": json.dumps(registro["qualidade"], ensure_ascii=False,
                                     separators=(",", ":")),
            "fonte": FONTE, "licenca": LICENCA,
        }
        escritor.writerow(linha)
    return buffer.getvalue().encode("utf-8-sig")


_I = IndicadorOperacional
_ORDEM = (_I.periodo_inicio.asc(), _I.linha_codigo.asc(), _I.sentido.asc(),
          _I.indicador.asc(), _I.id.asc())
_SEM_FILTROS = dict.fromkeys(("linha", "sentido", "tipo", "parada", "trecho", "versao_gtfs_id"))


def nome_arquivo_indicadores(dia):
    return f"indicadores_{dia.isoformat()}.csv"


def caminho_indicadores(dia):
    return diretorio_exportacoes() / "indicadores" / nome_arquivo_indicadores(dia)


def publicar_csv_indicadores(dia, agora):
    """Publica atomicamente o CSV completo após a consolidação US23-T03."""
    if isinstance(dia, datetime) or not isinstance(dia, date):
        raise TypeError("dia deve ser datetime.date")
    if agora.tzinfo is None:
        raise ValueError("agora deve conter fuso horário")
    chave = f"US23-T03:CONSOLIDACAO_DIA:{dia.isoformat()}"
    execucao = ExecucaoProcessamento.query.filter_by(chave_logica=chave, estado="PRONTO").one_or_none()
    if execucao is None:
        raise ValueError("o dia precisa estar consolidado antes da publicação")
    existente = arquivo_indicadores_completo(dia)
    if existente is not None and existente["recibo"]["execucao_id"] == execucao.id:
        return existente["recibo"]

    inicio, fim = periodo_utc(dia)
    filtros = {"linha": None, "sentido": None, "tipo": None, "parada": None, "trecho": None,
               "versao_gtfs_id": None, "inicio": inicio, "fim": fim}
    registros = filtrar_periodo(consulta_base(filtros), filtros).order_by(*_ORDEM).all()
    conteudo = _serializar_linhas(registros)
    destino = caminho_indicadores(dia)
    _gravar_atomico(destino, conteudo)
    recibo = {
        "data": dia.isoformat(), "gerado_em": agora.astimezone(timezone.utc).isoformat(),
        "execucao_id": execucao.id, "execucao_chave": chave,
        "sha256": hashlib.sha256(conteudo).hexdigest(), "tamanho_bytes": len(conteudo),
        "registros": len(registros), "fonte": FONTE, "licenca": LICENCA,
    }
    gravar_json(Path(f"{destino}.publicacao.json"), recibo)
    return recibo


def _gravar_atomico(destino, conteudo):
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destino.name}.", suffix=".tmp",
                                         dir=destino.parent, delete=False) as arquivo:
            temporario = Path(arquivo.name)
            arquivo.write(conteudo)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.chmod(temporario, 0o644)
        os.replace(temporario, destino)
        temporario = None
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)


def arquivo_indicadores_completo(dia):
    """Confere recibo, hash e consolidação antes de expor o arquivo ao público."""
    destino = caminho_indicadores(dia)
    recibo_path = Path(f"{destino}.publicacao.json")
    try:
        recibo = json.loads(recibo_path.read_text(encoding="utf-8"))
        conteudo = destino.read_bytes()
        execucao = ExecucaoProcessamento.query.filter_by(
            chave_logica=recibo["execucao_chave"], estado="PRONTO").one_or_none()
        if (recibo["data"] != dia.isoformat() or execucao is None
                or recibo["execucao_id"] != execucao.id
                or recibo["sha256"] != hashlib.sha256(conteudo).hexdigest()
                or recibo["tamanho_bytes"] != len(conteudo)):
            return None
        return {"caminho": destino, "recibo": recibo, "conteudo": conteudo}
    except (OSError, KeyError, ValueError, TypeError):
        return None


def disponibilidade_csv():
    """Metadados que acompanham o arquivo baixado e seus cabeçalhos HTTP."""
    return {
        "fonte": FONTE,
        "fonte_url": FONTE_URL,
        "licenca": LICENCA,
        "licenca_url": LICENCA_URL,
        "condicoes_fonte": CONDICOES_FONTE,
        "condicoes_fonte_url": CONDICOES_FONTE_URL,
        "disponibilidade": ("CSV do dia D (America/Sao_Paulo) publicado após a consolidação, "
                            "a partir de 04:00 e até 06:00 de D+1; antes disso o download responde 404"),
        "tipos_compostos": {"ESPERA": ["ESPERA_ESTIMADA", "EXCESSO_ESPERA"],
                            "FREQUENCIA": ["FREQUENCIA_OBSERVADA", "FREQUENCIA_PROGRAMADA"]},
        "codificacao": "UTF-8 com BOM",
        "separador": ",",
        "nulos": "campos vazios",
        "fuso": "instantes ISO 8601 com offset preservado da API",
        "fuso_publicacao": "America/Sao_Paulo",
        "url": "/downloads/indicadores?formato=csv&data=AAAA-MM-DD",
        # US25-T01: exportação da versão exibida no painel (/series → versao_dados.id).
        "versao_dados": ("Com versao_dados, exporta exatamente a versão exibida em /series, mesmo após "
                         "novas publicações; versão inexistente responde 404 e versão não preservada 410. "
                         "Prazo de retenção ainda não definido pelo PO."),
    }


def _instante(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def filtrar_linhas(linhas, filtros, tipos=None, *, serie=False):
    """Filtros do download US10 sobre linhas do CSV. ``serie=True`` reproduz a seleção
    de ``GET /series`` (US23-T01): janelas inteiramente contidas no período e na faixa
    diária e, sem parada/trecho, somente resultados da linha inteira."""
    inicio, fim = filtros["inicio"], filtros["fim"]
    faixas = (list(_janelas_horarias(inicio, fim, filtros["hora_inicio"], filtros["hora_fim"]))
              if serie and filtros.get("hora_inicio") else None)
    linha_inteira = serie and filtros["parada"] is None and filtros["trecho"] is None
    for registro in linhas:
        if any(filtros[campo] is not None and registro[campo] != filtros[campo]
               for campo in ("linha", "sentido", "parada", "trecho")):
            continue
        if tipos is not None and registro["tipo"] not in tipos:
            continue
        if (filtros["versao_gtfs_id"] is not None
                and registro["versao_gtfs_id"].lower() != filtros["versao_gtfs_id"]):
            continue
        a, b = _instante(registro["inicio"]), _instante(registro["fim"])
        if serie:
            if not (inicio <= a and b <= fim) or (linha_inteira and (registro["parada"] or registro["trecho"])):
                continue
            if faixas is not None and not any(x <= a and b <= y for x, y in faixas):
                continue
        else:
            if inicio is not None and b <= inicio:
                continue
            if fim is not None:
                pontual = inicio is not None and fim == inicio
                if (pontual and a > fim) or (not pontual and a >= fim):
                    continue
        yield registro


# --- US25-T01: versão preservada do conjunto exibido no painel ------------------------
#
# A US23-T03 publica conjuntos inteiros (ExecucaoProcessamento ATUALIZACAO/CONSOLIDACAO),
# mas a consolidação reescreve as janelas provisórias e remove o que não reproduz. Por
# isso cada publicação grava, antes do commit, o estado completo do seu período. A versão
# exibida é a última publicação vigente; exportá-la recompõe o período a partir desses
# arquivos imutáveis, nunca da tabela atual.

class VersaoIndisponivel(Exception):
    def __init__(self, codigo, mensagem, status, **extra):
        super().__init__(mensagem)
        self.codigo, self.status, self.extra = codigo, status, extra


def caminho_conjunto(execucao_id):
    return diretorio_exportacoes() / "indicadores" / "conjuntos" / f"{execucao_id}.csv"


def preservar_conjunto(execucao):
    """Chamado por ``agenda._publicar`` após o flush e ANTES do commit: uma falha aqui
    desfaz a publicação, logo toda execução PRONTO nova tem o seu arquivo."""
    registros = (consulta_base(_SEM_FILTROS)
                 .filter(_I.periodo_inicio >= execucao.periodo_inicio,
                         _I.periodo_fim <= execucao.periodo_fim)
                 .order_by(*_ORDEM).all())
    conteudo = _serializar_linhas(registros)
    destino = caminho_conjunto(execucao.id)
    _gravar_atomico(destino, conteudo)
    gravar_json(Path(f"{destino}.publicacao.json"), {
        "execucao_id": execucao.id, "execucao_chave": execucao.chave_logica, "tipo": execucao.tipo,
        "periodo_inicio": execucao.periodo_inicio.isoformat(),
        "periodo_fim": execucao.periodo_fim.isoformat(),
        "publicada_em": execucao.finalizada_em.isoformat(),
        "sha256": hashlib.sha256(conteudo).hexdigest(), "tamanho_bytes": len(conteudo),
        "registros": len(registros), "fonte": FONTE, "licenca": LICENCA,
    })


def _linhas_preservadas(execucao):
    destino = caminho_conjunto(execucao.id)
    try:
        recibo = json.loads(Path(f"{destino}.publicacao.json").read_text(encoding="utf-8"))
        conteudo = destino.read_bytes()
        if (recibo["execucao_id"] != execucao.id or recibo["execucao_chave"] != execucao.chave_logica
                or recibo["sha256"] != hashlib.sha256(conteudo).hexdigest()):
            return None
        return list(csv.DictReader(io.StringIO(conteudo.decode("utf-8-sig"))))
    except (OSError, KeyError, ValueError, TypeError, csv.Error):
        return None


def _segmentos(execucoes, inicio, fim):
    """[(a, b, execução)]: cada trecho de [inicio, fim) pertence à publicação mais
    recente (na ordem da versão) que o cobriu; trechos sem publicação ficam de fora."""
    livres, segmentos = [(inicio, fim)], []
    for execucao in reversed(execucoes):
        restantes = []
        for a, b in livres:
            x, y = max(a, execucao.periodo_inicio), min(b, execucao.periodo_fim)
            if x >= y:
                restantes.append((a, b))
                continue
            segmentos.append((x, y, execucao))
            restantes += [(p, q) for p, q in ((a, x), (y, b)) if p < q]
        livres = restantes
    return segmentos


def linhas_da_versao(versao_id, filtros, tipos=None):
    """Linhas do CSV exatamente como estavam publicadas na versão ``versao_id``.

    Levanta ``VersaoIndisponivel``: 404 se a versão não existe; 410 se o arquivo de
    alguma publicação necessária faltar/estiver corrompido, ou se a versão continha
    registros publicados fora da agenda (não preservados). Nunca troca de versão."""
    E = ExecucaoProcessamento
    versao = E.query.filter(E.id == versao_id, E.estado == "PRONTO",
                            E.tipo.in_(TIPOS_PUBLICACAO)).one_or_none()
    if versao is None:
        raise VersaoIndisponivel("VERSAO_INEXISTENTE",
                                 "Não existe publicação de indicadores com esta versao_dados.", 404,
                                 versao_dados=versao_id)
    inicio, fim = filtros["inicio"], filtros["fim"]
    limite = ordem_publicacao(versao)
    execucoes = sorted((e for e in E.query.filter(
        E.estado == "PRONTO", E.tipo.in_(TIPOS_PUBLICACAO), E.finalizada_em <= versao.finalizada_em,
        E.periodo_fim > inicio, E.periodo_inicio < fim) if ordem_publicacao(e) <= limite),
        key=ordem_publicacao)
    segmentos = _segmentos(execucoes, inicio, fim)

    # Registros que existiam na versão mas não estão em nenhum arquivo preservado
    # (publicados fora da agenda, ou depois do arquivo do seu trecho).
    preservado = _ou_balanceado([and_(_I.periodo_inicio >= a, _I.periodo_fim <= b,
                                      _I.publicado_em <= e.finalizada_em)
                                 for a, b, e in segmentos]) if segmentos else False
    fora = consulta_base(filtros).filter(_I.periodo_inicio >= inicio, _I.periodo_fim <= fim,
                                         _I.publicado_em <= versao.finalizada_em, not_(preservado))
    if tipos is not None:
        fora = fora.filter(_I.indicador.in_(sorted(tipos)))
    if filtros["parada"] is None and filtros["trecho"] is None:  # mesma regra de /series
        fora = fora.filter(_I.parada_codigo.is_(None), _I.trecho_id.is_(None))
    fora = fora.with_entities(_I.id).first()
    if fora is not None:
        raise VersaoIndisponivel(
            "VERSAO_INDISPONIVEL", "Esta versão contém indicadores do período que não foram "
            "preservados; não é possível reproduzi-la.", 410,
            versao_dados=versao.id, motivo="PERIODO_NAO_PRESERVADO")

    linhas, arquivos = [], {}
    for a, b, execucao in sorted(segmentos, key=lambda s: s[0]):
        if execucao.id not in arquivos:
            arquivos[execucao.id] = _linhas_preservadas(execucao)
        if arquivos[execucao.id] is None:
            raise VersaoIndisponivel(
                "VERSAO_INDISPONIVEL", "O conjunto preservado desta versão não está mais "
                "disponível ou não confere com o recibo.", 410,
                versao_dados=versao.id, motivo="ARQUIVO_AUSENTE_OU_INVALIDO", publicacao=execucao.id)
        linhas += [r for r in arquivos[execucao.id]
                   if a <= _instante(r["inicio"]) and _instante(r["fim"]) <= b]
    linhas.sort(key=lambda r: (_instante(r["inicio"]), r["linha"], r["sentido"], r["tipo"]))
    return versao, linhas
