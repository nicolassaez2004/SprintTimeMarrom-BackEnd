from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.analises.associacoes import digest
from app.analises.plano import (PlanoNaoLiberado, exigir_plano_aprovado, hash_conteudo_plano, verificar_prontidao)

RAIZ=Path(__file__).resolve().parents[2]


@pytest.fixture
def entrada():
    return (json.loads((RAIZ/'docs/analises/US21-plano-v1.json').read_text(encoding='utf-8')),
            json.loads((RAIZ/'docs/validacoes/US21-T01-amostra.json').read_text(encoding='utf-8')))


def test_proposta_sem_aceite_nao_calcula_nem_libera_estimacao(entrada):
    plano,base=entrada; r=verificar_prontidao(plano,[base])
    assert not r['pode_iniciar_estimacao'] and not r['pode_publicar_resultados']
    assert r['calculos_de_associacao_executados']==0
    assert 'PLANO_NAO_APROVADO' in r['motivos']
    assert 'ACEITE_INCOMPLETO_ANALISTA_INSTITUICAO' in r['motivos']
    with pytest.raises(PlanoNaoLiberado): exigir_plano_aprovado(plano,[base])


def test_amostra_controlada_tem_um_mes_e_uma_exposicao_compartilhada(entrada):
    plano,base=entrada; r=verificar_prontidao(plano,[base])
    assert r['observacoes']==4 and r['meses']==1
    assert 'BASE_SINTETICA_APENAS_ENSAIO' in r['motivos']
    assert 'MENOS_DE_DOIS_MESES' in r['motivos'] and 'SEM_VARIACAO_CHUVA' in r['motivos']


def test_escrever_aprovado_nao_substitui_aceites_e_decisoes(entrada):
    p,b=entrada; p['situacao']='APROVADO'
    r=verificar_prontidao(p,[b])
    assert not r['pode_iniciar_estimacao']
    assert any(m.startswith('ACEITE_INCOMPLETO_') for m in r['motivos'])
    assert 'PARAMETRO_PENDENTE_BLOCO_TEMPORAL_MESES' in r['motivos']


def test_aceite_vinculado_a_outra_versao_nao_serve(entrada):
    p,b=entrada; h=hash_conteudo_plano(p)
    # Registro FICTÍCIO exclusivamente para testar integridade, não um aceite real.
    for a in p['aprovacoes'].values(): a.update(nome='FICTICIO_TESTE',instituicao='FICTICIA',data='2026-11-27',referencia='urn:teste:nao-e-aceite',hash_conteudo_plano=h)
    p['incerteza']['nivel']=.90
    r=verificar_prontidao(p,[b])
    assert 'ACEITE_DE_OUTRA_VERSAO_ANALISTA_INSTITUICAO' in r['motivos']
    assert 'ACEITE_DE_OUTRA_VERSAO_PO' in r['motivos']


def test_valor_da_base_alterado_invalida_hash(entrada):
    p,b=entrada; b['linhas'][0]['desempenho_s']=999
    assert 'HASH_BASE_INVALIDO' in verificar_prontidao(p,[b])['motivos']


def test_repetir_a_base_nao_aumenta_amostra_independente(entrada):
    p,b=entrada
    assert 'OBSERVACOES_REPETIDAS' in verificar_prontidao(p,[b,deepcopy(b)])['motivos']


def test_versoes_gtfs_distintas_nao_formam_painel_silenciosamente(entrada):
    p,b=entrada; outra=deepcopy(b); outra['selecao']['versao_gtfs_id']='outra'
    outra['hash_base']=digest({k:v for k,v in outra.items() if k!='hash_base'})
    assert 'REDES_OU_CADEIAS_INCOMPATIVEIS' in verificar_prontidao(p,[b,outra])['motivos']


def test_dividir_mesma_estacao_em_clusters_falsos_e_bloqueado(entrada):
    p,b=entrada
    p['agrupamentos']['mapa_espacial']={r['versao_gtfs_id']+'|'+r['trecho_id']:r['trecho_id'] for r in b['linhas']}
    p['agrupamentos']['referencia_mapa_espacial']='urn:mapa-ficticio:teste'
    assert 'ESTACAO_COMPARTILHADA_DIVIDIDA_EM_BLOCOS' in verificar_prontidao(p,[b])['motivos']


def test_chuva_parcial_nao_e_exposicao_primaria_completa(entrada):
    p,b=entrada
    p['agrupamentos']['mapa_espacial']={r['versao_gtfs_id']+'|'+r['trecho_id']:'bloco-unico' for r in b['linhas']}
    b['linhas'][0]['clima']['cobertura']=.99
    assert 'CHUVA_PRIMARIA_INCOMPLETA' in verificar_prontidao(p,[b])['motivos']


def test_politica_e_lista_de_bases_precisam_corresponder_ao_plano(entrada):
    p,b=entrada; p['qualidade']['hash_politica_base_aprovada']='outro'; p['qualidade']['hashes_bases_aprovadas']=['outro']
    r=verificar_prontidao(p,[b])
    assert 'POLITICA_DIVERGENTE_DO_PLANO' in r['motivos'] and 'BASES_DIVERGENTES_DO_PLANO' in r['motivos']


def test_vazio_fica_sem_dados_e_nao_recebe_coeficiente_zero(entrada):
    p,_=entrada; r=verificar_prontidao(p,[])
    assert 'SEM_BASES' in r['motivos'] and 'SEM_OBSERVACOES_ELEGIVEIS' in r['motivos']
    assert not any(k in r for k in ('coeficiente','p_valor','ic'))
