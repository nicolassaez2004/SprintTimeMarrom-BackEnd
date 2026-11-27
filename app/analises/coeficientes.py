"""US21: modelos associacionais; resultados exigem diagnóstico e revisão humana."""
from collections import Counter
import math
import platform

import numpy as np
import scipy
from scipy.stats import t
import statsmodels
from statsmodels.regression.linear_model import OLS
from statsmodels.stats.sandwich_covariance import cov_cluster_2groups

from app.analises.associacoes import digest
from app.analises.plano import verificar_prontidao

VERSAO_CALCULO = 'us21-ols-cluster-2vias-1.1'

LIMITACOES = [
    'Associação observacional não demonstra causalidade.',
    'Dependência residual entre blocos e influência de grupos ainda exigem diagnóstico e revisão.',
    'A aproximação t e a correção de amostra finita não garantem cobertura nominal com poucos grupos.',
    'Velocidade dos ônibus é proxy de tráfego e pode compartilhar medições com o desempenho.',
]


def calcular_associacoes(plano, bases):
    """Estima apenas bases e parâmetros vinculados ao plano aprovado.

    Uma estimativa calculada não é uma autorização de publicação: diagnósticos
    de dependência, influência e revisão institucional continuam necessários.
    """
    prontidao = verificar_prontidao(plano, bases)
    registro = {'versao_calculo': VERSAO_CALCULO, 'hash_plano': prontidao['hash_conteudo_plano'],
                'hashes_bases': prontidao['hashes_bases'], 'prontidao': prontidao,
                'pode_publicar_resultados': False,
                'versoes': {'python': platform.python_version(), 'numpy': np.__version__,
                            'scipy': scipy.__version__, 'statsmodels': statsmodels.__version__}}
    if not prontidao['pode_iniciar_estimacao']:
        return {**registro, 'situacao': 'NAO_LIBERADO', 'modelos': {}}
    # Falha fechada se o plano mudou em relação à especificação implementada.
    modelo = plano['modelos_propostos']
    incerteza = plano['incerteza']
    if (modelo.get('estimador') != 'OLS_ASSOCIACIONAL' or modelo.get('pesos') != 'IGUAIS_POR_TRECHO_MES'
            or modelo.get('principal') != 'desempenho_s ~ chuva_mm_observada/10 + C(linha_sentido) + C(infra_classificacao) + C(mes_calendario)'
            or modelo.get('secundario') != 'principal + trafego.velocidade_kmh_espacial_candidata'
            or incerteza.get('metodo_proposto') != 'COVARIANCIA_CLUSTER_DUAS_VIAS'
            or incerteza.get('nivel') != .95 or incerteza.get('correcao_amostra_pequena') is not True
            or incerteza.get('referencia_critica_proposta') != 't com min(G_espacial,G_temporal)-1 graus de liberdade; confirmar com analista'
            or plano['qualidade'].get('cobertura_chuva_primaria') != 1.0):
        return {**registro, 'situacao': 'ESPECIFICACAO_NAO_SUPORTADA', 'modelos': {}}
    linhas = sorted((r for b in bases for r in b['linhas'] if r['incluida']), key=lambda r: r['id_observacao'])
    agrup = plano['agrupamentos']
    resultados = {nome: _ajustar(linhas, agrup, secundaria) for nome, secundaria in
                  (('principal', False), ('secundario', True))}
    return {**registro, 'situacao': 'CALCULADO' if all(r['situacao'] == 'CALCULADO' for r in resultados.values()) else 'INCOMPLETO',
            'parametros': {'nivel_confianca': .95, 'correcao_amostra_pequena': True,
                           'bloco_temporal_meses': agrup['bloco_temporal_meses'],
                           'referencia_mapa_espacial': agrup['referencia_mapa_espacial'],
                           'hash_mapa_espacial': digest(agrup['mapa_espacial']),
                           'metodo_incerteza': 'COVARIANCIA_CLUSTER_DUAS_VIAS',
                           'referencia_critica': 't com min(G_espacial,G_temporal)-1 graus de liberdade',
                           'origem_blocos_temporais': 'ano * 12 + mes - 1, divisão inteira pela duração',
                           'min_observacoes': agrup['min_observacoes'], 'min_grupos_espaciais': agrup['min_grupos_espaciais'],
                           'min_grupos_temporais': agrup['min_grupos_temporais']}, 'modelos': resultados}


def _ajustar(linhas, agrup, secundaria):
    """Núcleo determinístico, também exercitado com dados sintéticos conhecidos."""
    def valido(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    completos = []
    for r in linhas:
        trafego = (r.get('trafego') or {}).get('velocidade_kmh_espacial_candidata')
        if (valido(r.get('desempenho_s')) and valido(r.get('chuva_mm_observada'))
                and r.get('linha') is not None and r.get('sentido') is not None and r.get('infra_classificacao')
                and (not secundaria or valido(trafego))):
            completos.append(r)
    n = len(completos)
    info = {'observacoes': n, 'excluidas_por_dados_ausentes': len(linhas)-n}
    def falha(motivo):
        return {**info, 'situacao': 'DADOS_INSUFICIENTES_OU_FALHA', 'motivo': motivo}
    if n < agrup['min_observacoes'] or not n:
        return falha('AMOSTRA_INSUFICIENTE')
    try:
        espacial = [agrup['mapa_espacial'][r['versao_gtfs_id']+'|'+r['trecho_id']] for r in completos]
        temporal = [(int(r['mes'][:4])*12+int(r['mes'][5:7])-1)//agrup['bloco_temporal_meses'] for r in completos]
        grupos_e = {v:i for i,v in enumerate(sorted(set(espacial)))}
        grupos_t = {v:i for i,v in enumerate(sorted(set(temporal)))}
        ge, gt = len(grupos_e), len(grupos_t)
        info.update(grupos_espaciais=ge, grupos_temporais=gt,
                    tamanhos_grupos_espaciais=dict(sorted(Counter(espacial).items())),
                    tamanhos_grupos_temporais={str(k): v for k,v in sorted(Counter(temporal).items())})
        if ge < max(2, agrup['min_grupos_espaciais']) or gt < max(2, agrup['min_grupos_temporais']):
            return falha('GRUPOS_INSUFICIENTES')
        nomes = ['intercepto', 'chuva_10mm']
        colunas = [np.ones(n), np.array([r['chuva_mm_observada']/10 for r in completos])]
        categorias = [('linha_sentido', [(str(r['linha']), str(r['sentido'])) for r in completos]),
                      ('infra_classificacao', [r['infra_classificacao'] for r in completos]),
                      ('mes_calendario', [r['mes'][5:7] for r in completos])]
        referencias = {}
        for nome, valores in categorias:
            niveis = sorted(set(valores))
            referencias[nome] = niveis[0]
            for nivel in niveis[1:]:
                nomes.append(f'{nome}={nivel}')
                colunas.append(np.array([float(v == nivel) for v in valores]))
        if secundaria:
            nomes.append('velocidade_kmh')
            colunas.append(np.array([r['trafego']['velocidade_kmh_espacial_candidata'] for r in completos]))
        x = np.column_stack(colunas)
        y = np.array([r['desempenho_s'] for r in completos])
        info.update(parametros_estimados=len(nomes), categorias_referencia=referencias)
        if n <= x.shape[1] or np.linalg.matrix_rank(x) != x.shape[1]:
            return falha('MATRIZ_SEM_POSTO_COMPLETO_OU_GRAUS_DE_LIBERDADE')
        ajuste = OLS(y, x, missing='raise').fit()
        cov = cov_cluster_2groups(ajuste, np.array([grupos_e[v] for v in espacial]),
                                  np.array([grupos_t[v] for v in temporal]), use_correction=True)[0]
        if not np.all(np.isfinite(ajuste.params)) or not np.all(np.isfinite(cov)):
            return falha('RESULTADO_NAO_FINITO')
        if np.linalg.eigvalsh(cov).min() < 0 or np.any(np.diag(cov) <= 0):
            return falha('COVARIANCIA_INVALIDA')
        graus = min(ge, gt)-1
        critico = t.ppf(.975, graus)
        erros = np.sqrt(np.diag(cov))
        resultados = {nome: {'coeficiente': float(coef), 'erro_padrao': float(erro),
                             'ic95': [float(coef-critico*erro), float(coef+critico*erro)],
                             'p_valor': float(2*t.sf(abs(coef/erro), graus))}
                      for nome, coef, erro in zip(nomes, ajuste.params, erros)}
        if not math.isfinite(critico) or any(
                not all(math.isfinite(v) for v in (r['coeficiente'], r['erro_padrao'], *r['ic95'], r['p_valor']))
                or r['ic95'][0] > r['coeficiente'] or r['coeficiente'] > r['ic95'][1]
                for r in resultados.values()):
            return falha('INTERVALO_INVALIDO')
        return {**info, 'situacao': 'CALCULADO', 'graus_liberdade': graus, 'valor_critico': float(critico),
                'limitacoes': list(LIMITACOES), 'coeficientes': resultados}
    except (ValueError, KeyError, TypeError, ZeroDivisionError, np.linalg.LinAlgError, FloatingPointError) as exc:
        return falha('ERRO_AJUSTE: ' + type(exc).__name__)
