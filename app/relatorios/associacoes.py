"""Registro interno e consulta de snapshots US21, sem executar ajustes em HTTP."""
from copy import deepcopy
from datetime import date
import math

from app import db
from app.analises.associacoes import digest
from app.indicadores.propagacao import limites_mes
from app.models import RelatorioAssociacaoMensal


def _texto(valor):
    return isinstance(valor, str) and bool(valor.strip())


def _numero(valor):
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _resultado_valido(resultado):
    if not isinstance(resultado, dict) or resultado.get('situacao') != 'CALCULADO':
        return False
    if not all(_texto(resultado.get(c)) for c in ('versao_calculo', 'hash_plano')):
        return False
    if resultado['versao_calculo'] != 'us21-ols-cluster-2vias-1.1':
        return False  # Não atribuir a uma versão desconhecida o método abaixo.
    prontidao = resultado.get('prontidao') or {}
    if not isinstance(prontidao, dict) or prontidao.get('pode_iniciar_estimacao') is not True or prontidao.get('motivos') != []:
        return False
    hashes = resultado.get('hashes_bases')
    if not isinstance(hashes, list) or not hashes or not all(_texto(h) for h in hashes):
        return False
    versoes = resultado.get('versoes')
    if not isinstance(versoes, dict) or not all(_texto(versoes.get(k)) for k in ('python', 'numpy', 'scipy', 'statsmodels')):
        return False
    parametros = resultado.get('parametros') or {}
    if not isinstance(parametros, dict) or parametros.get('nivel_confianca') != .95 or parametros.get('metodo_incerteza') != 'COVARIANCIA_CLUSTER_DUAS_VIAS':
        return False
    modelos = resultado.get('modelos')
    if not isinstance(modelos, dict) or set(modelos) != {'principal', 'secundario'}:
        return False
    for modelo in modelos.values():
        if not isinstance(modelo, dict) or modelo.get('situacao') != 'CALCULADO': return False
        if type(modelo.get('observacoes')) is not int or modelo['observacoes'] < 1: return False
        if not isinstance(modelo.get('limitacoes'), list) or not modelo['limitacoes'] or not all(_texto(v) for v in modelo['limitacoes']): return False
        coeficientes = modelo.get('coeficientes')
        if not isinstance(coeficientes, dict) or not coeficientes: return False
        for c in coeficientes.values():
            if not isinstance(c, dict): return False
            ic = c.get('ic95')
            if not isinstance(ic, list) or len(ic) != 2: return False
            if not all(_numero(v) for v in [c.get('coeficiente'), c.get('erro_padrao'), c.get('p_valor'), *ic]): return False
            if c['erro_padrao'] <= 0 or not 0 <= c['p_valor'] <= 1 or not ic[0] <= c['coeficiente'] <= ic[1]: return False
    return True


def _revisao_valida(revisao, resultado):
    if not isinstance(revisao, dict): return False
    if not all(_texto(revisao.get(k)) for k in ('responsavel', 'data', 'referencia', 'hash_resultado')): return False
    try:
        date.fromisoformat(revisao['data'])
    except ValueError:
        return False
    return (revisao['hash_resultado'] == digest(resultado)
            and revisao.get('diagnosticos_aprovados') is True
            and revisao.get('publicacao_aprovada') is True)


def registrar_publicado(mes, resultado, bases, revisao):
    """Adiciona snapshot revisado à transação corrente; não faz commit.

    `mes` é a referência do relatório; o ajuste usa todo o painel descrito
    nas bases, não apenas o mês de referência. Revisão é decisão humana.
    """
    limites_mes(mes)
    if not _resultado_valido(resultado) or not _revisao_valida(revisao, resultado):
        raise ValueError('Resultado inválido ou revisão de publicação ausente/incompatível.')
    if not isinstance(bases, list) or not bases:
        raise ValueError('Bases de origem são obrigatórias.')
    for b in bases:
        if b.get('origem', '').startswith('SINTETICA'):
            raise ValueError('Base sintética não pode ser publicada como relatório real.')
        if b.get('hash_base') != digest({k: v for k, v in b.items() if k != 'hash_base'}):
            raise ValueError('Hash da base inválido.')
    if sorted(b['hash_base'] for b in bases) != sorted(resultado['hashes_bases']):
        raise ValueError('Bases não correspondem ao resultado.')
    if mes not in {b.get('mes') for b in bases}:
        raise ValueError('Mês de referência não pertence ao painel de entrada.')
    fontes = [{'mes': b['mes'], 'hash_base': b['hash_base'], 'versao_base': b['versao_base'],
               'selecao': deepcopy(b['selecao']), 'politica': deepcopy(b['politica'])} for b in bases]
    dados = {'mes': mes, 'situacao': 'PUBLICADO', 'fontes': sorted(fontes, key=lambda f: f['mes']),
             'metodo': {'estimador': 'OLS_ASSOCIACIONAL', 'pesos': 'IGUAIS_POR_TRECHO_MES',
                        'desfecho': 'AUMENTO_ATRASO_TRECHO_MENSAL', 'metrica': 'MEDIA', 'unidade': 's',
                        'interpretacao': 'Associação condicional observacional, não causal.'},
             'resultado': deepcopy(resultado), 'revisao': deepcopy(revisao)}
    _inserir(mes, 'PUBLICADO', dados)


def registrar_invalido(mes, motivo):
    """Registra ausência de análise válida sem salvar coeficientes parciais."""
    limites_mes(mes)
    if not _texto(motivo): raise ValueError('Informe o motivo da análise inválida.')
    dados = {'mes': mes, 'situacao': 'INVALIDO', 'motivo': motivo}
    _inserir(mes, 'INVALIDO', dados)


def _inserir(mes, situacao, dados):
    existente = db.session.get(RelatorioAssociacaoMensal, mes)
    if existente is not None and existente.situacao == 'INVALIDO' and situacao == 'PUBLICADO':
        # Uma tentativa inválida pode ser substituída após cálculo e revisão válidos.
        existente.situacao, existente.dados, existente.hash_dados = situacao, dados, digest(dados)
        from app.models import agora_utc
        existente.registrado_em = agora_utc()
        return
    if existente is not None:
        raise ValueError('Já existe relatório para o mês; preservação de versão exige revisão explícita.')
    db.session.add(RelatorioAssociacaoMensal(mes=mes, situacao=situacao, dados=dados, hash_dados=digest(dados)))


def consultar(mes):
    registro = db.session.get(RelatorioAssociacaoMensal, mes)
    if registro is None:
        return {'mes': mes, 'situacao': 'NAO_ENCONTRADO', 'mensagem': 'Não há relatório para o mês solicitado.'}, 404
    dados = registro.dados
    try:
        integridade = (isinstance(dados, dict) and digest(dados) == registro.hash_dados
                       and dados.get('mes') == mes and dados.get('situacao') == registro.situacao)
        if not integridade:
            raise ValueError('integridade')
        if registro.situacao == 'INVALIDO':
            if not _texto(dados.get('motivo')): raise ValueError('motivo ausente')
            return {'mes': mes, 'situacao': 'INVALIDO', 'motivo': dados['motivo']}, 409
        if (registro.situacao != 'PUBLICADO' or not _resultado_valido(dados.get('resultado'))
                or not _revisao_valida(dados.get('revisao'), dados['resultado']) or not dados.get('fontes')
                or not isinstance(dados.get('metodo'), dict) or dados['metodo'].get('estimador') != 'OLS_ASSOCIACIONAL'):
            raise ValueError('relatório inválido')
    except (ValueError, TypeError, KeyError):
        return {'mes': mes, 'situacao': 'INVALIDO', 'motivo': 'Registro inconsistente ou análise não liberada.'}, 409
    resposta = deepcopy(dados)
    resposta['registrado_em'] = registro.registrado_em.isoformat()
    # O campo do cálculo significa apenas que o cálculo não publica por si só.
    # A liberação de publicação está na revisão separada.
    return resposta, 200
