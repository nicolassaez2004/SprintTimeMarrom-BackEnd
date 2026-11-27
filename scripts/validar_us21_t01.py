"""Amostra SINTÉTICA da US21-T01; não consulta a Aiven nem calcula associações."""
import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app import db
from app.analises.associacoes import (PoliticaExclusao, SelecaoBase, digest, montar_base_mensal)
from app.contexto.infraestrutura.associacao import conjunto_de
from app.indicadores.propagacao import INDICADOR_MENSAL, INDICADOR_VARIACAO, limites_mes
from app.models import (AssociacaoInfraestruturaTrecho, EstacaoMeteorologica, IndicadorOperacional,
                        MedicaoContexto, ObservacaoClima, Trecho, TrechoEstacaoClima, VersaoInfraestrutura)
from scripts.validar_us19_t04 import REDE, ambiente_controlado, semear_ranking

AGORA = datetime(2026,12,2,tzinfo=timezone.utc)  # Relógio do ensaio; não é data de produção.
VERSOES = {'gtfs':REDE,'estimativa':'estimativa-controle','reconstrucao':'recon-controle',
           'correspondencia':'match-controle','calculo':'us08-controle','agregado':'us08-t02-controle'}


def preparar(app):
    semear_ranking(app)
    inicio,fim=limites_mes('2026-11')
    snap=VersaoInfraestrutura(id='infra-us21',fonte='GeoSampa',url_fonte='urn:controle:sintetico',
        versao_fonte='controle-outubro',sha256='c'*64,crs_origem='EPSG:4326',
        capturada_em=inicio-timedelta(days=1),metadados={'categoria':'CORREDOR','origem':'SINTETICA'})
    est=EstacaoMeteorologica(id='est-us21',fonte='INMET',codigo='SINT-21',nome='Estação SINTÉTICA',
        latitude=-23.55,longitude=-46.7,intervalo_seg=3600,versao_fonte='catalogo-controle',atualizada_em=AGORA)
    db.session.add_all([snap,est]); db.session.flush()
    conjunto=conjunto_de([snap.id])
    for t in Trecho.query.order_by(Trecho.id):
        db.session.add(TrechoEstacaoClima(trecho_id=t.id,fonte='INMET',versao_gtfs_id=REDE,
            linha_codigo=t.linha_codigo,estacao_id=est.id,situacao='ASSOCIADO',distancia_m=10,
            raio_maximo_m=25000,crs_medicao='EPSG:31983',versao_regra='us06-t03-v2',calculado_em=AGORA))
        db.session.add(AssociacaoInfraestruturaTrecho(id='infra-'+t.id,trecho_id=t.id,versao_conjunto=conjunto,
            versoes_infraestrutura=[snap.id],classificacao='CORREDOR',fracao_corredor=1,fracao_faixa=0,
            fracao_sem_cobertura=0,categorias_confirmadas=['CORREDOR'],feicoes_associadas=[],
            regra={'versao':'us07-t02-espacial-1.1','origem':'SINTETICA'},calculada_em=AGORA))
        for i,(distancia,duracao) in enumerate(((1000,180),(1000,90))):
            a=inicio+timedelta(minutes=15*i)
            db.session.add(MedicaoContexto(id=f'{t.id}-janela-{i}',chave=sha256(f'{t.id}:{i}'.encode()).hexdigest(),
                tipo='TRAFEGO',trecho_id=t.id,inicio=a,fim=a+timedelta(minutes=15),fonte='VELOCIDADE_OBSERVADA_ONIBUS',
                versao_fonte='trafego-controle',coletada_em=AGORA,unidades={'velocidade_media_kmh':'km/h','distancia_total_m':'m','duracao_total_seg':'s'},
                dados={'velocidade_media_kmh':3.6*distancia/duracao,'distancia_total_m':distancia,'duracao_total_seg':duracao,
                    'metodo_media':'ESPACIAL','motivo':None,'versoes':VERSOES,'origem':'SINTETICA',
                    'qualidade':{'falhas_coleta':{'incompleto':False,'percentual_falhas':0}}}))
    for i in range(1,721):
        db.session.add(ObservacaoClima(estacao_id=est.id,instante=inicio+timedelta(hours=i),intervalo_seg=3600,
            chuva=2 if i==1 else 3 if i==2 else 0,temperatura=20,fonte='INMET',versao_fonte='clima-controle',coletada_em=AGORA))
    for p in IndicadorOperacional.query.filter(IndicadorOperacional.indicador.in_((INDICADOR_MENSAL,INDICADOR_VARIACAO))):
        p.qualidade={**p.qualidade,'falhas_coleta':{'incompleto':False,'percentual_falhas':0},'origem':'SINTETICA'}
        p.publicado_em=AGORA
        p.id=str(uuid5(NAMESPACE_URL,'us21-controle:'+p.chave_resultado))
    db.session.commit()
    selecao=SelecaoBase(REDE,'MEDIA','trafego-controle',VERSOES,conjunto,('infra-us21',),'clima-controle','us06-t03-v2')
    # Limiar baixo EXCLUSIVO dos dois intervalos artificiais; não é recomendação operacional.
    politica=PoliticaExclusao('CONTROLE_SINTETICO_DUAS_JANELAS',.5,.95,1/2880,1,0)
    return selecao,politica


def executar():
    with ambiente_controlado() as app:
        selecao,politica=preparar(app)
        base=montar_base_mensal('2026-11',selecao,politica,agora=AGORA)
        assert base['resumo']['candidatas']==5 and base['resumo']['incluidas']==4
        assert base['resumo']['multiplicacao_de_observacoes']==0
        assert all(r['chuva_mm_observada']==5 for r in base['linhas'])
        assert all(abs(r['trafego']['velocidade_kmh_espacial_candidata']-80/3)<1e-10 for r in base['linhas'])
        base['origem']='SINTETICA_CONTROLADA'
        base['hash_base']=digest({k:v for k,v in base.items() if k!='hash_base'})
        return base


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saida',type=Path)
    args=parser.parse_args(); base=executar()
    if args.saida:
        args.saida.parent.mkdir(parents=True,exist_ok=True)
        args.saida.write_text(json.dumps(base,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'resumo':base['resumo'],'hash_base':base['hash_base'],'origem':'SINTETICA_CONTROLADA','apto_para_inferencia':False}))
