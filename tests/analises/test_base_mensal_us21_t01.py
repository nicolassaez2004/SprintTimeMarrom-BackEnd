from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.analises.associacoes import (PoliticaExclusao, carregar_fontes, construir_base, digest, montar_base_mensal)
from app.models import IndicadorOperacional, MedicaoContexto, ObservacaoClima
from scripts.validar_us19_t04 import ambiente_controlado
from scripts.validar_us21_t01 import AGORA, preparar


@pytest.fixture
def entrada():
    with ambiente_controlado() as app:
        selecao,politica=preparar(app)
        yield selecao,politica,carregar_fontes('2026-11',selecao)


def construir(entrada):
    s,p,f=entrada
    return construir_base('2026-11',s,p,f,agora=AGORA)


def linha(base,tid='T-AB'):
    return next(r for r in base['linhas'] if r['trecho_id']==tid)


def test_juncao_real_720_horas_dez_janelas_cinco_trechos_sem_multiplicar(entrada):
    base=construir(entrada)
    assert base['resumo']['candidatas']==5 and base['resumo']['incluidas']==4
    assert base['resumo']['registros_entrada']['clima']==720
    assert base['resumo']['multiplicacao_de_observacoes']==0
    assert len({r['id_observacao'] for r in base['linhas']})==5
    assert all(r['chuva_mm_observada']==5 for r in base['linhas'])
    assert base['resumo']['estacoes_distintas_incluidas']==1  # Cinco trechos não são cinco estações independentes.


def test_media_espacial_manual_e_nao_media_de_medias(entrada):
    r=linha(construir(entrada))
    assert r['desempenho_s']==80
    assert r['trafego']['distancia_m']==2000 and r['trafego']['duracao_s']==270
    assert r['trafego']['velocidade_kmh_espacial_candidata']==pytest.approx(80/3)
    assert r['trafego']['velocidade_kmh_espacial_candidata']!=30
    assert r['trafego']['cobertura_temporal']==pytest.approx(2/2880)


def test_zero_negativo_e_null_preservados(entrada):
    base=construir(entrada)
    assert linha(base,'T-DE')['desempenho_s']==0
    assert linha(base,'T-CD')['desempenho_s']==-30
    assert linha(base,'T-EF')['desempenho_s'] is None and not linha(base,'T-EF')['incluida']


def test_servico_apenas_leitura_e_fontes_imutaveis(entrada):
    s,p,f=entrada; original=deepcopy(f)
    antes=[m.query.count() for m in (IndicadorOperacional,MedicaoContexto,ObservacaoClima)]
    base=montar_base_mensal('2026-11',s,p,agora=AGORA)
    assert antes==[m.query.count() for m in (IndicadorOperacional,MedicaoContexto,ObservacaoClima)]
    assert not db.session.new and not db.session.dirty
    base['fontes']['clima'][0]['chuva']=999
    assert f==original


def test_duas_revisoes_de_desempenho_excluem_sem_escolher_uma(entrada):
    s,p,f=entrada
    outro=deepcopy(next(r for r in f['desempenho'] if r['trecho_id']=='T-AB'))
    outro['id']='outra-revisao'; outro['versao_conjunto']='v2'; outro['valor']=999
    f['desempenho'].append(outro)
    base=construir(entrada); r=linha(base)
    assert len(base['linhas'])==5 and r['desempenho_s'] is None
    assert 'DESEMPENHO_AMBIGUO' in r['exclusoes'] and len(r['referencias']['desempenho'])==2


def test_trafego_duplicado_nao_produz_media_silenciosa(entrada):
    s,p,f=entrada
    outro=deepcopy(next(r for r in f['trafego'] if r['trecho_id']=='T-AB'))
    outro['id']='duplicado'; outro['chave']='outra-chave'
    f['trafego'].append(outro)
    r=linha(construir(entrada))
    assert 'TRAFEGO_AMBIGUO' in r['exclusoes'] and r['trafego']['janelas_validas']==1


def test_versao_de_trafego_nao_selecionada_nao_contamina(entrada):
    s,p,f=entrada
    outro=deepcopy(next(r for r in f['trafego'] if r['trecho_id']=='T-AB'))
    outro['id']='outra-versao'; outro['dados']['versoes']['calculo']='v2'; outro['dados']['distancia_total_m']=99999
    f['trafego'].append(outro)
    r=linha(construir(entrada))
    assert r['trafego']['distancia_m']==2000 and r['trafego']['registros_fora_da_selecao']==1


@pytest.mark.parametrize('mutar',[
    lambda r:r['dados']['qualidade']['falhas_coleta'].update(incompleto=True),
    lambda r:r['dados'].update(motivo='metodo_media_indefinido'),
    lambda r:r['unidades'].update(velocidade_media_kmh='m/s'),
    lambda r:r.update(fim=(datetime.fromisoformat(r['inicio'])+timedelta(minutes=30)).isoformat()),
])
def test_janela_trafego_incompativel_e_contada_como_nao_valida(entrada,mutar):
    s,p,f=entrada
    for r in f['trafego']:
        if r['trecho_id']=='T-AB': mutar(r)
    r=linha(construir(entrada))
    assert r['trafego']['distancia_m'] is None and 'TRAFEGO_COBERTURA' in r['exclusoes']


def test_chuva_ausente_nao_vira_total_mensal_zero(entrada):
    s,p,f=entrada
    for r in f['clima']: r['chuva']=None
    base=construir(entrada)
    assert all(r['chuva_mm_observada'] is None and 'CLIMA_COBERTURA' in r['exclusoes'] for r in base['linhas'])


def test_lacuna_tem_denominador_civil_fixo_e_soma_parcial_identificada(entrada):
    s,p,f=entrada
    next(r for r in f['clima'] if r['chuva']==2)['chuva']=None
    r=linha(construir(entrada))
    assert r['chuva_mm_observada']==3 and r['clima']['esperadas']==720
    assert r['clima']['recebidas']==719 and r['clima']['cobertura']==pytest.approx(719/720)


def test_fim_nativo_climatico_incluido_inicio_anterior_excluido(entrada):
    s,p,f=entrada
    assert sum(r['chuva'] for r in f['clima'])==5
    extra=deepcopy(f['clima'][0]); extra['id']=9999; extra['instante']='2026-11-01T03:00:00+00:00'; extra['chuva']=100
    f['clima'].append(extra)
    r=linha(construir(entrada))
    assert r['chuva_mm_observada']==5 and r['clima']['recebidas']==720


def test_observacao_climatica_duplicada_torna_so_a_estacao_ambigua(entrada):
    """Revisão: duas medições no mesmo instante não são somadas nem derrubam a base inteira."""
    s,p,f=entrada; f['clima'].append(deepcopy(f['clima'][0]))
    base=construir(entrada)
    assert base['resumo']['candidatas']==5 and base['resumo']['incluidas']==0
    assert all('CLIMA_AMBIGUO' in r['exclusoes'] and r['chuva_mm_observada'] is None for r in base['linhas'])
    assert next(iter(base['clima_por_estacao'].values()))['instantes_repetidos']==1


def test_periodo_disponivel_identifica_base_inferior_ao_mes(entrada):
    periodo=construir(entrada)['resumo']['periodo_disponivel']
    assert periodo['dias_do_mes']==30
    assert periodo['clima']['dias_com_dados']==30 and periodo['clima']['inferior_ao_mes'] is False
    assert periodo['trafego']['inferior_ao_mes'] is True and periodo['inferior_ao_mes'] is True


def test_snapshot_futuro_nao_explica_mes_passado(entrada):
    s,p,f=entrada; f['snapshots'][0]['capturada_em']='2026-11-15T03:00:00+00:00'
    assert all('INFRA_SNAPSHOT_POSTERIOR_AO_INICIO' in r['exclusoes'] for r in construir(entrada)['linhas'])


def test_mudanca_infra_no_meio_do_mes_impede_atribuir_classe_unica(entrada):
    s,p,f=entrada
    novo=deepcopy(f['snapshots'][0]); novo.update(id='infra-v2',capturada_em='2026-11-15T03:00:00+00:00')
    f['snapshots'].append(novo)
    assert all('INFRA_MUDOU_NO_MES' in r['exclusoes'] for r in construir(entrada)['linhas'])


def test_sem_cobertura_infra_nao_e_via_comum(entrada):
    s,p,f=entrada
    for r in f['infra']: r.update(classificacao='SEM_COBERTURA',fracao_corredor=0,fracao_sem_cobertura=1)
    assert all(r['infra_classificacao']=='SEM_COBERTURA' and 'INFRA_COBERTURA' in r['exclusoes'] for r in construir(entrada)['linhas'])


def test_vinculo_climatico_bloqueado(entrada):
    s,p,f=entrada
    for r in f['vinculos']: r['situacao']='FORA_DO_RAIO'
    assert all('CLIMA_VINCULO_INDISPONIVEL' in r['exclusoes'] for r in construir(entrada)['linhas'])


def test_versoes_reconstrucao_compativeis_sao_obrigatorias(entrada):
    s,p,f=entrada
    for r in f['desempenho']: r['qualidade']['versoes_entrada'][0]['versao_reconstrucao']='outra'
    assert all('CADEIAS_DE_VERSOES_INCOMPATIVEIS' in r['exclusoes'] for r in construir(entrada)['linhas'])


def test_politica_estrita_nao_disfarca_duas_janelas_como_mes_completo(entrada):
    s,p,f=entrada
    base=construir((s,replace(p,cobertura_temporal_trafego_min=.95),f))
    assert base['resumo']['incluidas']==0
    assert all('TRAFEGO_COBERTURA' in r['exclusoes'] for r in base['linhas'])


def test_hash_estavel_independe_da_ordem_do_banco_e_congela_referencias(entrada):
    s,p,f=entrada; a=construir(entrada)
    for rs in f.values(): rs.reverse()
    b=construir(entrada)
    assert a['hash_base']==b['hash_base']
    assert a['hash_base']==digest({k:v for k,v in a.items() if k!='hash_base'})
    assert a['apto_para_inferencia'] is False


def test_mes_em_curso_e_relogio_sem_fuso_sao_rejeitados(entrada):
    s,p,f=entrada
    for agora in (datetime(2026,11,27,tzinfo=timezone.utc),datetime(2026,12,2)):
        with pytest.raises(ValueError): construir_base('2026-11',s,p,f,agora=agora)


def test_politica_sem_parametros_validos_nao_e_inventada():
    with pytest.raises(ValueError): PoliticaExclusao('x',.9,float('nan'),.9,1,0)
    with pytest.raises(ValueError): PoliticaExclusao('x',True,.9,.9,1,0)


@pytest.mark.parametrize('instante',['2026-12-01T03:02:00+00:00','2026-11-01T02:59:59+00:00'])
def test_dia_operacional_nao_pode_ser_cruzado_com_contexto_civil_diferente(entrada,instante):
    s,p,f=entrada
    origem=next(r for r in f['variacoes'] if r['trecho_id']=='T-AB' and r['valor'] is not None)['qualidade']['passagem_origem_id']
    next(r for r in f['passagens'] if r['id']==origem)['instante']=instante
    r=linha(construir(entrada))
    assert not r['incluida'] and 'DESEMPENHO_MES_OPERACIONAL_DIVERGE_DO_CIVIL' in r['exclusoes']


def test_agregado_sem_evidencia_original_nao_alega_alinhamento(entrada):
    s,p,f=entrada
    f['passagens']=[r for r in f['passagens'] if r['parada_codigo']!='A']
    r=linha(construir(entrada))
    assert not r['incluida'] and 'DESEMPENHO_ORIGENS_NAO_VERIFICAVEIS' in r['exclusoes']


def test_amostras_originais_devem_bater_com_denominador_publicado(entrada):
    s,p,f=entrada
    f['variacoes']=[r for r in f['variacoes'] if not (r['trecho_id']=='T-AB' and r['valor']==180)]
    assert 'DESEMPENHO_ORIGENS_NAO_VERIFICAVEIS' in linha(construir(entrada))['exclusoes']
