"""US21-T02: verificação prévia, sem executar coeficientes, IC ou p-valores.

Verifica completude/integridade do registro de aprovação; não autentica a
identidade de uma pessoa. A validade institucional da evidência é revisão humana.
"""
from collections import defaultdict
from datetime import date

from app.analises.associacoes import VERSAO_BASE, digest, numero


class PlanoNaoLiberado(ValueError):
    pass


def hash_conteudo_plano(plano):
    # Nome/data/link de aceite podem ser anexados sem alterar o conteúdo assinado.
    return digest({k:v for k,v in plano.items() if k not in ('aprovacoes','situacao')})


def verificar_prontidao(plano, bases):
    motivos=[]
    hp=hash_conteudo_plano(plano)
    if plano.get('situacao')!='APROVADO': motivos.append('PLANO_NAO_APROVADO')
    for papel in ('analista_instituicao','po'):
        a=plano.get('aprovacoes',{}).get(papel,{})
        campos=('nome','data','referencia','hash_conteudo_plano') + (('instituicao',) if papel=='analista_instituicao' else ())
        if not all(isinstance(a.get(c),str) and a[c].strip() for c in campos):
            motivos.append('ACEITE_INCOMPLETO_'+papel.upper()); continue
        try: date.fromisoformat(a['data'])
        except ValueError: motivos.append('DATA_ACEITE_INVALIDA_'+papel.upper())
        if a['hash_conteudo_plano']!=hp: motivos.append('ACEITE_DE_OUTRA_VERSAO_'+papel.upper())
    agrup=plano.get('agrupamentos',{}); qualidade=plano.get('qualidade',{})
    for campo in ('bloco_temporal_meses','min_observacoes','min_grupos_espaciais','min_grupos_temporais'):
        if type(agrup.get(campo)) is not int or agrup[campo]<1: motivos.append('PARAMETRO_PENDENTE_'+campo.upper())
    mapa=agrup.get('mapa_espacial') or {}
    if not mapa or not agrup.get('referencia_mapa_espacial'): motivos.append('MAPA_ESPACIAL_PENDENTE')
    if not qualidade.get('hash_politica_base_aprovada'): motivos.append('POLITICA_BASE_PENDENTE')
    if not bases: motivos.append('SEM_BASES')
    linhas=[]; hashes=[]; redes=set(); cadeias=set()
    for b in bases:
        h=b.get('hash_base'); hashes.append(h)
        if h!=digest({k:v for k,v in b.items() if k!='hash_base'}): motivos.append('HASH_BASE_INVALIDO')
        if b.get('versao_base')!=VERSAO_BASE: motivos.append('VERSAO_BASE_INCOMPATIVEL')
        if b.get('origem','').startswith('SINTETICA'): motivos.append('BASE_SINTETICA_APENAS_ENSAIO')
        if digest(b['politica'])!=qualidade.get('hash_politica_base_aprovada'): motivos.append('POLITICA_DIVERGENTE_DO_PLANO')
        s=b['selecao']; redes.add(s['versao_gtfs_id'])
        cadeias.add(digest({k:s[k] for k in ('metrica_desempenho','trafego_versao_fonte','trafego_versoes','clima_versao_fonte','clima_regra_vinculo')}))
        if s['metrica_desempenho']!=plano.get('variavel_alvo',{}).get('metrica_proposta'): motivos.append('DESFECHO_DIVERGENTE_DO_PLANO')
        linhas.extend(r for r in b['linhas'] if r['incluida'])
    if sorted(hashes)!=sorted(qualidade.get('hashes_bases_aprovadas') or []): motivos.append('BASES_DIVERGENTES_DO_PLANO')
    if len(redes)>1 or len(cadeias)>1: motivos.append('REDES_OU_CADEIAS_INCOMPATIVEIS')
    if len({r['id_observacao'] for r in linhas})!=len(linhas): motivos.append('OBSERVACOES_REPETIDAS')
    meses={r['mes'] for r in linhas}
    if len(meses)<2: motivos.append('MENOS_DE_DOIS_MESES')
    if len({r['chuva_mm_observada'] for r in linhas})<2: motivos.append('SEM_VARIACAO_CHUVA')
    if not linhas: motivos.append('SEM_OBSERVACOES_ELEGIVEIS')
    grupos=set(); por_estacao=defaultdict(set); por_trecho=defaultdict(set)
    for r in linhas:
        chave=r['versao_gtfs_id']+'|'+r['trecho_id']; grupo=mapa.get(chave)
        if not isinstance(grupo,str) or not grupo: motivos.append('TRECHO_SEM_BLOCO_ESPACIAL'); continue
        grupos.add(grupo); por_estacao[r['estacao_id']].add(grupo); por_trecho[chave].add(grupo)
        if not numero(r['clima']['cobertura']) or r['clima']['cobertura']<qualidade.get('cobertura_chuva_primaria',1): motivos.append('CHUVA_PRIMARIA_INCOMPLETA')
    if any(len(g)>1 for g in por_estacao.values()): motivos.append('ESTACAO_COMPARTILHADA_DIVIDIDA_EM_BLOCOS')
    if any(len(g)>1 for g in por_trecho.values()): motivos.append('TRECHO_DIVIDIDO_EM_BLOCOS')
    bloco=agrup.get('bloco_temporal_meses')
    blocos_tempo={(int(m[:4])*12+int(m[5:])-1)//bloco for m in meses} if type(bloco) is int and bloco>0 else set()
    if len(grupos)<2 or len(blocos_tempo)<2: motivos.append('GRUPOS_INSUFICIENTES_PARA_DUAS_VIAS')
    for campo,n in (('min_observacoes',len(linhas)),('min_grupos_espaciais',len(grupos)),('min_grupos_temporais',len(blocos_tempo))):
        minimo=agrup.get(campo)
        if type(minimo) is int and n<minimo: motivos.append('AMOSTRA_ABAIXO_DO_PLANO_'+campo.upper())
    return {'hash_conteudo_plano':hp,'hashes_bases':hashes,'observacoes':len(linhas),'meses':len(meses),
        'grupos_espaciais':len(grupos),'grupos_temporais':len(blocos_tempo),'motivos':sorted(set(motivos)),
        'pode_iniciar_estimacao':not motivos,'pode_publicar_resultados':False,'calculos_de_associacao_executados':0,
        'verificacoes_posteriores_obrigatorias':['posto da matriz do modelo','dependência residual além dos blocos','validade da covariância e intervalos','influência e sensibilidades','revisão institucional dos resultados']}


def exigir_plano_aprovado(plano,bases):
    r=verificar_prontidao(plano,bases)
    if not r['pode_iniciar_estimacao']: raise PlanoNaoLiberado('; '.join(r['motivos']))
    return r
