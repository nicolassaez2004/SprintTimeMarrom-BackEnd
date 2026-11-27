"""US21-T01: uma observação por trecho/rede/mês, sem produto cartesiano.

Somente leitura. As seleções e a política são explícitas; não escolhe a versão
mais recente nem executa regressão/correlação. Agregações de contexto são
estatísticas descritivas de preparação, não estimativas de associação.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math

from app import db
from app.indicadores.propagacao import INDICADOR_MENSAL, INDICADOR_VARIACAO, limites_mes, versao_mensal
from app.models import (AssociacaoInfraestruturaTrecho, EstacaoMeteorologica, IndicadorOperacional,
                        MedicaoContexto, ObservacaoClima, PassagemEstimada, Trecho, TrechoEstacaoClima, VersaoInfraestrutura)

VERSAO_BASE = 'us21-base-mensal-1.0'
FONTE_TRAFEGO = 'VELOCIDADE_OBSERVADA_ONIBUS'


@dataclass(frozen=True)
class SelecaoBase:
    versao_gtfs_id: str
    metrica_desempenho: str
    trafego_versao_fonte: str
    trafego_versoes: dict
    infra_conjunto: str
    infra_versoes: tuple
    clima_versao_fonte: str
    clima_regra_vinculo: str
    fonte_clima: str = 'INMET'

    def __post_init__(self):
        if self.metrica_desempenho not in ('MEDIA', 'MEDIANA') or self.fonte_clima not in ('INMET', 'CGE'):
            raise ValueError('Selecione métrica mensal e fonte climática suportadas.')
        if not all((self.versao_gtfs_id, self.trafego_versao_fonte, self.infra_conjunto,
                    self.infra_versoes, self.clima_versao_fonte, self.clima_regra_vinculo)):
            raise ValueError('Versões devem ser fixadas explicitamente.')
        obrigatorias = {'gtfs','estimativa','reconstrucao','correspondencia','calculo','agregado'}
        if not obrigatorias <= self.trafego_versoes.keys() or not all(self.trafego_versoes[k] for k in obrigatorias):
            raise ValueError('Fixe todas as versões do tráfego.')
        if self.trafego_versoes['gtfs'] != self.versao_gtfs_id:
            raise ValueError('Tráfego e desempenho devem usar a mesma rede.')
        if len(set(self.infra_versoes)) != len(self.infra_versoes):
            raise ValueError('Snapshot de infraestrutura repetido.')


@dataclass(frozen=True)
class PoliticaExclusao:
    identificador: str
    cobertura_desempenho_min: float
    cobertura_chuva_min: float
    cobertura_temporal_trafego_min: float
    amostras_desempenho_min: int
    fracao_infra_sem_cobertura_max: float

    def __post_init__(self):
        for v in (self.cobertura_desempenho_min, self.cobertura_chuva_min,
                  self.cobertura_temporal_trafego_min, self.fracao_infra_sem_cobertura_max):
            if not numero(v) or not 0 <= v <= 1:
                raise ValueError('Coberturas e frações devem estar entre zero e um.')
        if not self.identificador or type(self.amostras_desempenho_min) is not int or self.amostras_desempenho_min < 1:
            raise ValueError('Identifique a política e informe amostra mínima positiva.')


def numero(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def canonico(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(v):
    return sha256(canonico(v).encode('utf-8')).hexdigest()


def _json(v):
    if isinstance(v, datetime): return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, date): return v.isoformat()
    if isinstance(v, dict): return {k:_json(x) for k,x in v.items()}
    if isinstance(v, (list, tuple)): return [_json(x) for x in v]
    if v is not None and not isinstance(v, (str,int,float,bool)): return float(v)
    return v


def _registro(r, campos):
    return {c:_json(getattr(r,c)) for c in campos.split()}


def _qualidade_ok(q):
    f = (q or {}).get('falhas_coleta') or {}
    return f.get('incompleto') is False and numero(f.get('percentual_falhas')) and 0 <= f['percentual_falhas'] <= 100


def carregar_fontes(mes, selecao):
    """Consultas separadas, sem join entre fatos horários, de 15 min e mensais."""
    inicio, fim = limites_mes(mes)
    I = IndicadorOperacional
    filtro = (I.indicador == INDICADOR_MENSAL, I.versao_gtfs_id == selecao.versao_gtfs_id,
              I.versao_calculo == versao_mensal(selecao.metrica_desempenho),
              I.periodo_inicio == inicio, I.periodo_fim == fim)
    with db.session.no_autoflush:
        desempenho = I.query.filter(*filtro).order_by(I.id).all()
        ids = {r.trecho_id for r in desempenho if r.trecho_id is not None}
        # Subconsulta evita uma lista IN com dezenas de milhares de parâmetros.
        cohort = db.session.query(I.trecho_id).filter(*filtro)
        variacoes = I.query.filter(I.indicador == INDICADOR_VARIACAO,
            I.trecho_id.in_(cohort), I.versao_gtfs_id == selecao.versao_gtfs_id,
            I.periodo_inicio >= inicio, I.periodo_inicio < fim).order_by(I.id).all()
        pass_ids = sorted({r.qualidade.get('passagem_origem_id') for r in variacoes if r.qualidade.get('passagem_origem_id')})
        passagens = []
        for pos in range(0,len(pass_ids),500):
            passagens.extend(PassagemEstimada.query.filter(PassagemEstimada.id.in_(pass_ids[pos:pos+500])).all())
        trechos = Trecho.query.filter(Trecho.id.in_(cohort)).order_by(Trecho.id).all()
        vinculos = TrechoEstacaoClima.query.filter(TrechoEstacaoClima.trecho_id.in_(cohort),
            TrechoEstacaoClima.fonte == selecao.fonte_clima).all()
        est_ids = {v.estacao_id for v in vinculos if v.estacao_id}
        estacoes = EstacaoMeteorologica.query.filter(EstacaoMeteorologica.id.in_(est_ids)).all()
        clima = ObservacaoClima.query.filter(ObservacaoClima.estacao_id.in_(est_ids),
            ObservacaoClima.instante > inicio, ObservacaoClima.instante <= fim).order_by(ObservacaoClima.id).all()
        trafego = MedicaoContexto.query.filter(MedicaoContexto.trecho_id.in_(cohort),
            MedicaoContexto.tipo == 'TRAFEGO', MedicaoContexto.fonte == FONTE_TRAFEGO,
            MedicaoContexto.inicio < fim, MedicaoContexto.fim > inicio).order_by(MedicaoContexto.id).all()
        infra = AssociacaoInfraestruturaTrecho.query.filter(AssociacaoInfraestruturaTrecho.trecho_id.in_(cohort),
            AssociacaoInfraestruturaTrecho.versao_conjunto == selecao.infra_conjunto).all()
        snapshots = VersaoInfraestrutura.query.filter(VersaoInfraestrutura.capturada_em < fim).all()
    if len(ids) != len(trechos): raise ValueError('Agregado mensal sem trecho rastreável ou trecho_id ausente.')
    campos = {
        'desempenho':'id trecho_id versao_gtfs_id linha_codigo sentido indicador valor unidade periodo_inicio periodo_fim contagem_numerador contagem_denominador versao_calculo versao_conjunto versao_correspondencia qualidade publicado_em',
        'passagens':'id viagem_observada_id versao_gtfs_id parada_codigo instante versao_estimativa posicao_anterior_id posicao_posterior_id',
        'trechos':'id versao_gtfs_id linha_codigo sentido trajeto ordem geometria',
        'vinculos':'trecho_id fonte estacao_id situacao distancia_m raio_maximo_m crs_medicao versao_regra calculado_em',
        'estacoes':'id fonte codigo intervalo_seg operacao_inicio versao_fonte atualizada_em latitude longitude',
        'clima':'id estacao_id instante intervalo_seg chuva temperatura unidade_chuva unidade_temperatura fonte versao_fonte coletada_em coleta_id',
        'trafego':'id chave trecho_id inicio fim fonte versao_fonte dados unidades coletada_em',
        'infra':'id trecho_id versao_conjunto versoes_infraestrutura classificacao fracao_corredor fracao_faixa fracao_sem_cobertura regra feicoes_associadas calculada_em',
        'snapshots':'id fonte versao_fonte sha256 capturada_em metadados',
    }
    campos['variacoes'] = campos['desempenho']
    return {nome:[_registro(r,campos[nome]) for r in registros] for nome,registros in {
        'desempenho':desempenho,'variacoes':variacoes,'passagens':passagens,'trechos':trechos,'vinculos':vinculos,'estacoes':estacoes,'clima':clima,
        'trafego':trafego,'infra':infra,'snapshots':snapshots}.items()}


def _agrupar(registros, campo):
    grupos = defaultdict(list)
    for r in registros: grupos[r[campo]].append(r)
    return grupos


def _clima(estacao, observacoes, inicio, fim, selecao):
    if not estacao: return {'chuva_mm_observada':None,'recebidas':0,'esperadas':None,'cobertura':None,'referencias':[]}
    passo = estacao['intervalo_seg']
    if passo not in (900,3600): raise ValueError('Grade climática não suportada.')
    esperado = int((fim-inicio).total_seconds()/passo)
    validas = [r for r in observacoes if r['fonte'] == selecao.fonte_clima
        and r['versao_fonte'] == selecao.clima_versao_fonte and r['intervalo_seg'] == passo
        and r['unidade_chuva'] == 'mm' and numero(r['chuva']) and r['chuva'] >= 0
        and inicio < datetime.fromisoformat(r['instante']) <= fim
        and datetime.fromisoformat(r['instante']).timestamp() % passo == 0]
    instantes = [r['instante'] for r in validas]
    if len(set(instantes)) != len(instantes): raise ValueError('Observação climática duplicada.')
    return {'chuva_mm_observada':sum(r['chuva'] for r in validas) if validas else None,
        'recebidas':len(validas),'esperadas':esperado,'cobertura':len(validas)/esperado,
        'descartadas_ou_nulas':len(observacoes)-len(validas),'referencias':[r['id'] for r in validas]}


def _trafego(registros, inicio, fim, selecao):
    escolhidos = [r for r in registros if r['versao_fonte'] == selecao.trafego_versao_fonte
        and {k:v for k,v in r['dados'].get('versoes',{}).items() if k != 'calculo_amostras'} == selecao.trafego_versoes]
    grupos = _agrupar(escolhidos, 'inicio')
    duplicadas = sum(len(rs)>1 for rs in grupos.values())
    validas = []
    for r in escolhidos:
        a,b = map(datetime.fromisoformat,(r['inicio'],r['fim']))
        d,u = r['dados'],r['unidades']
        if len(grupos[r['inicio']]) != 1 or not inicio <= a < b <= fim or b-a != timedelta(minutes=15) or a.timestamp()%900: continue
        if d.get('motivo') is not None or d.get('metodo_media') not in ('ARITMETICA','ESPACIAL'): continue
        if not all(numero(d.get(k)) and d[k]>=0 for k in ('velocidade_media_kmh','distancia_total_m','duracao_total_seg')) or d['duracao_total_seg']<=0: continue
        if any(u.get(k)!=v for k,v in {'velocidade_media_kmh':'km/h','distancia_total_m':'m','duracao_total_seg':'s'}.items()): continue
        if not _qualidade_ok(d.get('qualidade')): continue
        validas.append(r)
    distancia = sum(r['dados']['distancia_total_m'] for r in validas)
    duracao = sum(r['dados']['duracao_total_seg'] for r in validas)
    esperado = int((fim-inicio).total_seconds()/900)
    return {'fonte':FONTE_TRAFEGO,'proxy':True,'janelas_validas':len(validas),'janelas_esperadas_civis':esperado,
        'cobertura_temporal':len(validas)/esperado,'distancia_m':distancia if validas else None,
        'duracao_s':duracao if validas else None,'velocidade_kmh_espacial_candidata':3.6*distancia/duracao if validas else None,
        'janelas_ambiguas':duplicadas,'registros_selecionados':len(escolhidos),
        'registros_fora_da_selecao':len(registros)-len(escolhidos), 'referencias':[r['id'] for r in validas]}


def construir_base(mes, selecao, politica, fontes, *, agora):
    """Transformação determinística do snapshot de entrada; não acessa banco/rede."""
    if agora.tzinfo is None: raise ValueError('Informe agora com fuso.')
    inicio,fim = limites_mes(mes)
    if fim > agora: raise ValueError('Use mês encerrado; mês em curso não é base mensal completa.')
    if not isinstance(selecao,SelecaoBase) or not isinstance(politica,PoliticaExclusao): raise ValueError('Seleção e política explícitas são obrigatórias.')
    fontes={k:sorted(deepcopy(rs),key=canonico) for k,rs in fontes.items()}
    perf = _agrupar(fontes['desempenho'],'trecho_id'); traf = _agrupar(fontes['trafego'],'trecho_id')
    variacoes = _agrupar(fontes['variacoes'],'trecho_id'); passagens = {r['id']:r for r in fontes['passagens']}
    infra = _agrupar(fontes['infra'],'trecho_id'); vin = _agrupar(fontes['vinculos'],'trecho_id')
    obs = _agrupar(fontes['clima'],'estacao_id'); est = {e['id']:e for e in fontes['estacoes']}
    snapshots = {v['id']:v for v in fontes['snapshots']}
    selecionados = [snapshots.get(i) for i in selecao.infra_versoes]
    infra_ausente = any(v is None for v in selecionados)
    infra_futura = any(v and datetime.fromisoformat(v['capturada_em'])>inicio for v in selecionados)
    categorias = {(v['fonte'],v['metadados'].get('categoria')) for v in selecionados if v}
    infra_mudou = any((v['fonte'],v['metadados'].get('categoria')) in categorias
        and inicio < datetime.fromisoformat(v['capturada_em']) < fim and v['id'] not in selecao.infra_versoes for v in snapshots.values())
    climas = {eid:_clima(e,obs[eid],inicio,fim,selecao) for eid,e in est.items()}
    linhas = []
    for t in sorted(fontes['trechos'],key=lambda t:t['id']):
        motivos = []
        rs = perf[t['id']]; p = rs[0] if len(rs)==1 else None
        referencias_originais = []
        if len(rs)!=1: motivos.append('DESEMPENHO_AMBIGUO')
        if t['versao_gtfs_id']!=selecao.versao_gtfs_id: motivos.append('REDE_INCOMPATIVEL')
        if p:
            if (p['versao_gtfs_id'],p['linha_codigo'],p['sentido']) != (t['versao_gtfs_id'],t['linha_codigo'],t['sentido']): motivos.append('DESEMPENHO_ESCOPO_INCOMPATIVEL')
            if p['indicador']!=INDICADOR_MENSAL or p['versao_calculo']!=versao_mensal(selecao.metrica_desempenho) or datetime.fromisoformat(p['periodo_inicio'])!=inicio or datetime.fromisoformat(p['periodo_fim'])!=fim: motivos.append('DESEMPENHO_PERIODO_OU_VERSAO')
            if not numero(p['valor']) or p['unidade']!='s': motivos.append('DESEMPENHO_SEM_VALOR')
            c=p['qualidade'].get('cobertura')
            if not numero(c) or not politica.cobertura_desempenho_min<=c<=1: motivos.append('DESEMPENHO_COBERTURA')
            if not numero(p['contagem_numerador']) or p['contagem_numerador']<politica.amostras_desempenho_min: motivos.append('DESEMPENHO_AMOSTRA')
            n,d=p['contagem_numerador'],p['contagem_denominador']
            if not numero(d) or d<=0 or not numero(n) or not 0<=n<=d: motivos.append('DESEMPENHO_DENOMINADOR')
            elif not numero(c) or not math.isclose(c,n/d,abs_tol=1e-9): motivos.append('DESEMPENHO_CONTAGENS_INCOERENTES')
            entradas=p['qualidade'].get('versoes_entrada')
            if not isinstance(entradas,list) or len(entradas)!=1 or not isinstance(entradas[0],dict): motivos.append('DESEMPENHO_VERSOES_AUSENTES_OU_MISTURADAS')
            elif any(entradas[0].get(campo)!=selecao.trafego_versoes[k] for campo,k in (
                ('versao_gtfs_id','gtfs'),('versao_estimativa','estimativa'),
                ('versao_reconstrucao','reconstrucao'),('versao_correspondencia','correspondencia'))): motivos.append('CADEIAS_DE_VERSOES_INCOMPATIVEIS')
            if isinstance(entradas,list) and len(entradas)==1 and isinstance(entradas[0],dict):
                e=entradas[0]
                originais=[r for r in variacoes[t['id']] if r['versao_gtfs_id']==e.get('versao_gtfs_id')
                    and r['versao_calculo']==e.get('versao_calculo') and r['versao_correspondencia']==e.get('versao_correspondencia')
                    and r['qualidade'].get('versao_reconstrucao')==e.get('versao_reconstrucao')
                    and r['qualidade'].get('versao_estimativa')==e.get('versao_estimativa')]
                referencias_originais=[r['id'] for r in originais]
                calculaveis=[r for r in originais if r['valor'] is not None]
                if len(originais)!=d or len(calculaveis)!=n: motivos.append('DESEMPENHO_ORIGENS_NAO_VERIFICAVEIS')
                for original in calculaveis:
                    passagem=passagens.get(original['qualidade'].get('passagem_origem_id'))
                    if (not passagem or passagem['viagem_observada_id']!=original['qualidade'].get('viagem_observada_id')
                        or passagem['versao_gtfs_id']!=t['versao_gtfs_id'] or passagem['parada_codigo']!=original['qualidade'].get('parada_origem')
                        or passagem['versao_estimativa']!=original['qualidade'].get('versao_estimativa')):
                        motivos.append('DESEMPENHO_ORIGENS_NAO_VERIFICAVEIS'); continue
                    if not inicio<=datetime.fromisoformat(passagem['instante'])<fim:
                        motivos.append('DESEMPENHO_MES_OPERACIONAL_DIVERGE_DO_CIVIL')
            if not _qualidade_ok(p['qualidade']): motivos.append('DESEMPENHO_QUALIDADE')
        vs=vin[t['id']]; v=vs[0] if len(vs)==1 else None
        e=est.get(v['estacao_id']) if v else None
        clima=climas.get(v['estacao_id']) if v else None
        if not v or v['situacao']!='ASSOCIADO' or v['versao_regra']!=selecao.clima_regra_vinculo or not e or e['fonte']!=selecao.fonte_clima: motivos.append('CLIMA_VINCULO_INDISPONIVEL')
        if not clima or clima['chuva_mm_observada'] is None or clima['cobertura']<politica.cobertura_chuva_min: motivos.append('CLIMA_COBERTURA')
        tr=_trafego(traf[t['id']],inicio,fim,selecao)
        if tr['janelas_ambiguas']: motivos.append('TRAFEGO_AMBIGUO')
        if not tr['janelas_validas'] or tr['cobertura_temporal']<politica.cobertura_temporal_trafego_min: motivos.append('TRAFEGO_COBERTURA')
        ir=infra[t['id']]; i=ir[0] if len(ir)==1 else None
        if not i or set(i['versoes_infraestrutura'])!=set(selecao.infra_versoes) or i['versao_conjunto']!=selecao.infra_conjunto: motivos.append('INFRA_AUSENTE_OU_AMBIGUA')
        if i and (i['classificacao']=='SEM_COBERTURA' or i['fracao_sem_cobertura']>politica.fracao_infra_sem_cobertura_max): motivos.append('INFRA_COBERTURA')
        if infra_ausente: motivos.append('INFRA_SNAPSHOT_AUSENTE')
        if infra_futura: motivos.append('INFRA_SNAPSHOT_POSTERIOR_AO_INICIO')
        if infra_mudou: motivos.append('INFRA_MUDOU_NO_MES')
        chave={'mes':mes,'versao_gtfs_id':t['versao_gtfs_id'],'trecho_id':t['id']}
        linhas.append({**chave,'id_observacao':digest(chave),'linha':t['linha_codigo'],'sentido':t['sentido'],
            'inicio':inicio.isoformat(),'fim':fim.isoformat(),'incluida':not motivos,'exclusoes':sorted(set(motivos)),
            'desempenho_s':p['valor'] if p else None,'chuva_mm_observada':clima['chuva_mm_observada'] if clima else None,
            'clima':{k:v for k,v in clima.items() if k!='referencias'} if clima else None,
            'trafego':tr,'infra_classificacao':i['classificacao'] if i else None,
            'infra_fracao_corredor':i['fracao_corredor'] if i else None,'infra_fracao_faixa':i['fracao_faixa'] if i else None,
            'estacao_id':e['id'] if e else None,'referencias':{'desempenho':[r['id'] for r in rs],
                'variacoes_origem':referencias_originais,
                'infra':[r['id'] for r in ir],'vinculo_clima':v,'trafego_entrada':[r['id'] for r in traf[t['id']]]}})
    assert len({r['id_observacao'] for r in linhas})==len(linhas)==len(perf), 'Cardinalidade inesperada na base mensal.'
    incluidas=[r for r in linhas if r['incluida']]
    payload={'versao_base':VERSAO_BASE,'mes':mes,'fuso':'America/Sao_Paulo','grao':'trecho + versao_gtfs + mes',
        'selecao':asdict(selecao),'politica':asdict(politica),'situacao_politica':'PROPOSTA_PENDENTE_APROVACAO',
        'fontes':fontes,'clima_por_estacao':climas,'linhas':linhas,
        'dicionario':{
            'desempenho_s':{'unidade':'s','origem':'IndicadorOperacional.valor, AUMENTO_ATRASO_TRECHO_MENSAL','regra':'Valor publicado da métrica/versão selecionada, sem recalcular.'},
            'chuva_mm_observada':{'unidade':'mm','origem':'ObservacaoClima.chuva via TrechoEstacaoClima.estacao_id','regra':'Somar uma vez por janela nativa recebida, sem distribuir hora em quartos de hora ou preencher faltantes.'},
            'trafego.distancia_m':{'unidade':'m','origem':'MedicaoContexto.dados.distancia_total_m','regra':'Soma das janelas válidas da série fixada.'},
            'trafego.duracao_s':{'unidade':'s','origem':'MedicaoContexto.dados.duracao_total_seg','regra':'Soma das janelas válidas da série fixada.'},
            'trafego.velocidade_kmh_espacial_candidata':{'unidade':'km/h','origem':'3.6 * distancia_m / duracao_s','regra':'Estatística descritiva candidata; não média simples de médias e não velocidade do tráfego geral.'},
            'infra_classificacao':{'unidade':'categoria','origem':'AssociacaoInfraestruturaTrecho.classificacao','regra':'Conjunto de snapshots fixado e anterior ao mês, sem alteração intramensal.'},
            'infra_fracao_corredor':{'unidade':'razão','origem':'AssociacaoInfraestruturaTrecho.fracao_corredor','regra':'Preservada, sem inferir via comum da ausência de mapeamento.'},
            'infra_fracao_faixa':{'unidade':'razão','origem':'AssociacaoInfraestruturaTrecho.fracao_faixa','regra':'Preservada.'}},
        'resumo':{'candidatas':len(linhas),'incluidas':len(incluidas),'excluidas':len(linhas)-len(incluidas),
            'exclusoes_por_motivo':dict(sorted(Counter(m for r in linhas for m in r['exclusoes']).items())),
            'estacoes_distintas_incluidas':len({r['estacao_id'] for r in incluidas}),
            'linhas_distintas_incluidas':len({r['linha'] for r in incluidas}),
            'registros_entrada':{k:len(v) for k,v in fontes.items()},'multiplicacao_de_observacoes':0},
        'limitacoes':['Tráfego é proxy calculado dos mesmos ônibus: associação mecânica com desempenho é possível.',
            'Soma de chuva cobre somente horas recebidas; não é total mensal completo se houver lacunas.',
            'Denominador de tráfego é grade civil de 15 min do mês, não quantidade de janelas com operação esperada.',
            'Vínculo climático atual não tem histórico; snapshot registra estação/regra/coordenadas usados nesta extração.',
            'Trechos diferentes podem compartilhar estação, veículo e via; número de linhas não é amostra independente.',
            'Não identifica trechos equivalentes entre versões GTFS; não autoriza inferência ou conclusão causal.'],
        'apto_para_inferencia':False}
    payload['hash_base']=digest(payload)
    return payload


def montar_base_mensal(mes, selecao, politica, *, agora):
    return construir_base(mes,selecao,politica,carregar_fontes(mes,selecao),agora=agora)
