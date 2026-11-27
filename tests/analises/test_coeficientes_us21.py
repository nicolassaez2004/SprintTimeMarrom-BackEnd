from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from app.analises.associacoes import digest
from app.analises.coeficientes import _ajustar, calcular_associacoes
from app.analises.plano import hash_conteudo_plano


def base_controlada():
    # Chuva e velocidade são ortogonais aos resíduos por projeção: os
    # coeficientes conhecidos são exatos, com resíduos e covariância não nulos.
    rng = np.random.default_rng(42)
    linhas = []
    for mes in range(1, 9):
        for trecho in range(20):
            linhas.append({'id_observacao': f'{mes:02}-{trecho:02}', 'mes': f'{2017+mes}-01',
                           'trecho_id': str(trecho), 'versao_gtfs_id': 'rede', 'incluida': True,
                           'linha': 'L', 'sentido': '0', 'infra_classificacao': 'VIA',
                           'clima': {'cobertura': 1.0}, 'estacao_id': str(trecho),
                           'chuva_mm_observada': float(rng.uniform(0, 100)),
                           'trafego': {'velocidade_kmh_espacial_candidata': float(rng.uniform(5, 40))}})
    x = np.array([[1, r['chuva_mm_observada']/10, 
                   r['trafego']['velocidade_kmh_espacial_candidata']] for r in linhas])
    efeito_espacial = rng.normal(size=20)
    efeito_temporal = rng.normal(size=8)
    efeito_velocidade = rng.normal(size=20)
    residuo = np.array([efeito_espacial[int(r['trecho_id'])] +
                        efeito_temporal[int(r['mes'][:4])-2018] * r['chuva_mm_observada']/10 +
                        efeito_velocidade[int(r['trecho_id'])] * r['trafego']['velocidade_kmh_espacial_candidata']/10 +
                        rng.normal(scale=.2) for r in linhas])
    residuo -= x @ np.linalg.lstsq(x, residuo, rcond=None)[0]
    y = x @ np.array([4, 2, 0.5]) + residuo
    for r, valor in zip(linhas, y): r['desempenho_s'] = float(valor)
    agrup = {'mapa_espacial': {'rede|'+str(i): str(i) for i in range(20)}, 'bloco_temporal_meses': 12,
             'min_observacoes': 30, 'min_grupos_espaciais': 5, 'min_grupos_temporais': 5,
             'referencia_mapa_espacial': 'urn:teste'}
    return linhas, agrup


def test_base_conhecida_e_reprodutibilidade():
    linhas, agrup = base_controlada()
    resultado = _ajustar(linhas, agrup, True)
    assert resultado['situacao'] == 'CALCULADO', resultado
    assert resultado['coeficientes']['chuva_10mm']['coeficiente'] == pytest.approx(2, abs=1e-10)
    assert resultado['coeficientes']['velocidade_kmh']['coeficiente'] == pytest.approx(.5, abs=1e-10)
    assert resultado == _ajustar(linhas, agrup, True)


def test_amostra_e_matriz_singular_nao_produzem_coeficientes():
    linhas, agrup = base_controlada()
    pequeno = _ajustar(linhas[:4], agrup, False)
    assert pequeno['motivo'] == 'AMOSTRA_INSUFICIENTE' and 'coeficientes' not in pequeno
    for r in linhas: r['chuva_mm_observada'] = 10
    singular = _ajustar(linhas, agrup, False)
    assert singular['motivo'] == 'MATRIZ_SEM_POSTO_COMPLETO_OU_GRAUS_DE_LIBERDADE'
    assert 'coeficientes' not in singular


def test_covariancia_invalida_nao_produz_coeficientes(monkeypatch):
    linhas, agrup = base_controlada()
    monkeypatch.setattr('app.analises.coeficientes.cov_cluster_2groups', lambda ajuste, *a, **k: (-np.eye(len(ajuste.params)), None, None))
    resultado = _ajustar(linhas, agrup, True)
    assert resultado['motivo'] == 'COVARIANCIA_INVALIDA' and 'coeficientes' not in resultado


def test_bloqueio_real_e_integracao_com_plano_ficticio():
    raiz = Path(__file__).resolve().parents[2]
    plano = json.loads((raiz/'docs/analises/US21-plano-v1.json').read_text())
    base = json.loads((raiz/'docs/validacoes/US21-T01-amostra.json').read_text())
    assert calcular_associacoes(plano, [base])['situacao'] == 'NAO_LIBERADO'
    linhas, agrup = base_controlada()
    plano['agrupamentos'].update(agrup)
    bases = []
    for mes in sorted({r['mes'] for r in linhas}):
        b = {'versao_base': base['versao_base'], 'politica': {'teste': True},
             'selecao': deepcopy(base['selecao']), 'resumo': {'periodo_disponivel': {'inferior_ao_mes': False}},
             'linhas': [r for r in linhas if r['mes'] == mes]}
        b['selecao']['versao_gtfs_id'] = 'rede'
        b['hash_base'] = digest(b)
        bases.append(b)
    plano['qualidade']['hash_politica_base_aprovada'] = digest(bases[0]['politica'])
    plano['qualidade']['hashes_bases_aprovadas'] = [b['hash_base'] for b in bases]
    plano['situacao'] = 'APROVADO'
    h = hash_conteudo_plano(plano)
    for aceite in plano['aprovacoes'].values():
        aceite.update(nome='FICTICIO', instituicao='FICTICIA', data='2026-11-27', referencia='urn:teste', hash_conteudo_plano=h)
    resultado = calcular_associacoes(plano, bases)
    assert resultado['situacao'] == 'CALCULADO', resultado
    assert resultado['modelos']['secundario']['coeficientes']['chuva_10mm']['coeficiente'] == pytest.approx(2)
    assert resultado['parametros']['hash_mapa_espacial'] == digest(agrup['mapa_espacial'])
    assert resultado['parametros']['nivel_confianca'] == .95
    assert resultado['modelos']['secundario']['limitacoes']
    assert resultado['pode_publicar_resultados'] is False
    bases[0]['linhas'][0]['desempenho_s'] = 99
    assert calcular_associacoes(plano, bases)['situacao'] == 'NAO_LIBERADO'


def test_falha_numerica_nao_e_resultado_valido(monkeypatch):
    linhas, agrup = base_controlada()
    def falhar(*args, **kwargs):
        raise np.linalg.LinAlgError('falha de decomposição')
    monkeypatch.setattr('app.analises.coeficientes.OLS', falhar)
    resultado = _ajustar(linhas, agrup, False)
    assert resultado['motivo'] == 'ERRO_AJUSTE: LinAlgError'
    assert 'coeficientes' not in resultado


@pytest.mark.parametrize('secundaria', [False, True])
def test_intervalos_95_contra_calculo_matricial_independente(secundaria):
    """Referência Cameron-Gelbach-Miller: V_espacial + V_temporal - V_interseção.

    Cálculo direto por somas de escores, sem OLS nem cov_cluster do statsmodels.
    O quantil t(7) é fixado pela tabela de referência, independente do serviço.
    """
    linhas, agrup = base_controlada()
    resultado = _ajustar(linhas, agrup, secundaria)
    assert resultado['situacao'] == 'CALCULADO', resultado
    x = np.array([[1., r['chuva_mm_observada']/10] +
                  ([r['trafego']['velocidade_kmh_espacial_candidata']] if secundaria else [])
                  for r in linhas])
    y = np.array([r['desempenho_s'] for r in linhas])
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residuos = y - x @ beta
    escores = x * residuos[:, None]
    espacial = [r['trecho_id'] for r in linhas]
    temporal = [r['mes'] for r in linhas]
    intersecoes = list(zip(espacial, temporal))
    n, k = x.shape

    def carne(grupos):
        somas = {}
        for grupo, escore in zip(grupos, escores):
            somas[grupo] = somas.get(grupo, np.zeros(k)) + escore
        g = len(somas)
        return sum(np.outer(s, s) for s in somas.values()) * g/(g-1) * (n-1)/(n-k)

    inversa = np.linalg.inv(x.T @ x)
    cov = inversa @ (carne(espacial) + carne(temporal) - carne(intersecoes)) @ inversa
    erros = np.sqrt(np.diag(cov))
    critico_t7 = 2.364624
    nomes = ['intercepto', 'chuva_10mm'] + (['velocidade_kmh'] if secundaria else [])
    assert resultado['graus_liberdade'] == 7
    assert resultado['valor_critico'] == pytest.approx(critico_t7, abs=5e-7)
    for i, nome in enumerate(nomes):
        estimativa = resultado['coeficientes'][nome]
        assert estimativa['erro_padrao'] == pytest.approx(erros[i], rel=1e-10)
        assert estimativa['ic95'] == pytest.approx([beta[i] - critico_t7*erros[i], beta[i] + critico_t7*erros[i]], abs=1e-6)
    assert resultado['limitacoes']
    assert resultado == _ajustar(linhas, agrup, secundaria)


def test_intervalo_nao_finito_e_rejeitado(monkeypatch):
    linhas, agrup = base_controlada()
    monkeypatch.setattr('app.analises.coeficientes.t.ppf', lambda *args: float('nan'))
    resultado = _ajustar(linhas, agrup, True)
    assert resultado['motivo'] == 'INTERVALO_INVALIDO'
    assert 'coeficientes' not in resultado


def test_controles_categoricos_e_ausentes_contra_referencia_independente():
    """Linha/sentido, infraestrutura e mês calendário com dois níveis cada, mais linhas
    com chuva ausente e desempenho não finito. Referência: dummies montadas à mão
    (nível de referência = menor valor ordenado) e mínimos quadrados do NumPy."""
    rng = np.random.default_rng(7)
    linhas = []
    for ano in range(2018, 2026):
        mes = f'{ano}-{"01" if ano % 2 else "07"}'
        for trecho in range(20):
            linhas.append({'id_observacao': f'{ano}-{trecho:02}', 'mes': mes, 'trecho_id': str(trecho),
                           'versao_gtfs_id': 'rede', 'incluida': True,
                           'linha': 'L1' if trecho < 10 else 'L2', 'sentido': str(trecho % 2),
                           'infra_classificacao': 'CORREDOR' if trecho % 3 == 0 else 'VIA',
                           'chuva_mm_observada': float(rng.uniform(0, 100))})
    linhas[0]['chuva_mm_observada'] = None
    linhas[1]['chuva_mm_observada'] = float('nan')
    validas = linhas[2:]

    def dummies(r):
        return [float(r['mes'][5:7] == '07'), float(r['infra_classificacao'] == 'VIA'),
                float((r['linha'], r['sentido']) == ('L1', '1')), float((r['linha'], r['sentido']) == ('L2', '0')),
                float((r['linha'], r['sentido']) == ('L2', '1'))]
    x = np.array([[1., r['chuva_mm_observada']/10, *dummies(r)] for r in validas])
    ruido = rng.normal(size=len(validas)) + np.array([int(r['trecho_id']) % 4 for r in validas])
    ruido -= x @ np.linalg.lstsq(x, ruido, rcond=None)[0]
    verdade = np.array([10, 3, -4, 6, 1, -2, 5])
    for r, valor in zip(validas, x @ verdade + ruido):
        r['desempenho_s'] = float(valor)
    linhas[0]['desempenho_s'], linhas[1]['desempenho_s'] = 50.0, 50.0
    agrup = {'mapa_espacial': {f'rede|{i}': str(i) for i in range(20)}, 'bloco_temporal_meses': 12,
             'min_observacoes': 30, 'min_grupos_espaciais': 5, 'min_grupos_temporais': 5}

    resultado = _ajustar(linhas, agrup, False)
    assert resultado['situacao'] == 'CALCULADO', resultado
    assert (resultado['observacoes'], resultado['excluidas_por_dados_ausentes']) == (158, 2)
    assert resultado['categorias_referencia'] == {
        'linha_sentido': ('L1', '0'), 'infra_classificacao': 'CORREDOR', 'mes_calendario': '01'}
    beta = np.linalg.lstsq(x, np.array([r['desempenho_s'] for r in validas]), rcond=None)[0]
    nomes = ['intercepto', 'chuva_10mm', 'mes_calendario=07', 'infra_classificacao=VIA',
             "linha_sentido=('L1', '1')", "linha_sentido=('L2', '0')", "linha_sentido=('L2', '1')"]
    obtidos = resultado['coeficientes']
    assert sorted(obtidos) == sorted(nomes)
    for nome, referencia, conhecido in zip(nomes, beta, verdade):
        assert obtidos[nome]['coeficiente'] == pytest.approx(referencia, abs=1e-9)
        assert obtidos[nome]['coeficiente'] == pytest.approx(conhecido, abs=1e-9)
        inferior, superior = obtidos[nome]['ic95']
        assert inferior < conhecido < superior
