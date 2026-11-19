"""Dados e polígono sintéticos: não comprovam a cobertura de frota real."""
import json
from copy import deepcopy
from datetime import date,datetime,timedelta,timezone
from uuid import uuid4
import pytest
from pyproj import Transformer
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from app import create_app,db
from app.models import (ArquivoArmazenado,VersaoGTFS,Linha,Parada,Trecho,ExecucaoColeta,
                        RegistroBrutoGPS,ObservacaoTratada,CorrespondenciaCodigo)
from app.correspondencia.geografia import Geografia,PROJECAO
from app.correspondencia.tabelas import importar_tabela
from app.correspondencia.service import MatchingService
from app.correspondencia.relatorio import relatorio_diario

URL='/qualidade/correspondencias?data=2026-11-18'
AGORA=datetime(2026,11,18,15,tzinfo=timezone.utc)
INV=Transformer.from_crs('EPSG:31983','EPSG:4326',always_xy=True)
X,Y=PROJECAO.transform(-46.63,-23.55)
def xy(x=0,y=0):return list(INV.transform(X+x,Y+y))

@pytest.fixture
def contexto():
    app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite://','GTFS_STORAGE_DIR':None})
    with app.app_context():
        event.listen(db.engine,'connect',lambda c,_:c.execute('PRAGMA foreign_keys=ON'))
        db.create_all()
        yield app
        db.session.remove();db.engine.dispose()

@pytest.fixture
def geo():
    anel=[xy(-2000,-2000),xy(2000,-2000),xy(2000,2000),xy(-2000,2000),xy(-2000,-2000)]
    return Geografia(json.dumps({'type':'Polygon','coordinates':[anel]}).encode())

def rede(inicio=date(2026,11,1),fim=date(2026,11,18),codigo='1012-10',offset=0):
    digest=uuid4().hex*2
    a=ArquivoArmazenado(chave=digest,mime_type='application/zip',nome_original='feed.zip',tamanho_bytes=1,sha256=digest)
    v=VersaoGTFS(hash=digest,capturada_em=AGORA,vigencia_inicio=inicio,vigencia_fim=fim,arquivo=a)
    db.session.add_all([a,v]);db.session.flush()
    db.session.add(Linha(versao_gtfs_id=v.id,codigo=codigo,nome='Sintética'))
    for chave,p in [('A',xy(-500,offset)),('B',xy(500,offset))]:
        db.session.add(Parada(versao_gtfs_id=v.id,codigo=chave,nome=chave,longitude=p[0],latitude=p[1]))
    db.session.flush()
    db.session.add(Trecho(versao_gtfs_id=v.id,linha_codigo=codigo,sentido='0',trajeto='viagem1',ordem=1,
        parada_origem_codigo='A',parada_destino_codigo='B',geometria=[xy(-500,offset),xy(500,offset)]))
    db.session.commit()
    return v

def tabela(v,versao='ov-1',entradas=None):
    return dict(versao=versao,versao_gtfs_id=v.id,fonte='amostra sintética conferida',
        vigencia_inicio=v.vigencia_inicio.isoformat(),vigencia_fim=v.vigencia_fim.isoformat() if v.vigencia_fim else None,
        entradas=entradas or [dict(codigo_olho_vivo='1273',sentido_olho_vivo='1',linha_codigo='1012-10',sentido_gtfs='0')])

def bruto(y=0,x=0,idade=30,codigo='1273',sentido='1',coleta=AGORA):
    ciclo=ExecucaoColeta(estado='OK',novos_persistidos=1)
    db.session.add(ciclo);db.session.flush()
    lon,lat=xy(x,y)
    b=RegistroBrutoGPS(execucao_id=ciclo.id,latitude=lat,longitude=lon,horario_gps=coleta-timedelta(seconds=idade),
        coletado_em=coleta,linha_origem=codigo,sentido_origem=sentido,payload={'original':{'id':ciclo.id}})
    db.session.add(b);db.session.commit()
    return b

def processar(geo,b):
    o=MatchingService(geo).processar(b.id);db.session.commit();return o

def test_tabela_imutavel_idempotente_e_sem_destino_preservado(contexto):
    v=rede();dados=tabela(v);t=importar_tabela(dados)
    assert importar_tabela(dados).id==t.id and CorrespondenciaCodigo.query.count()==1
    with pytest.raises(ValueError,match='outro conteúdo'):importar_tabela({**dados,'fonte':'modificada'})
    importar_tabela(tabela(v,'ov-2',[dict(codigo_olho_vivo='999',sentido_olho_vivo='2',motivo='revisão necessária')]))
    assert CorrespondenciaCodigo.query.filter_by(codigo_olho_vivo='999').one().linha_codigo is None

def test_mesmo_codigo_por_data_e_gtfs_preserva_passado(contexto,geo):
    v1=rede();importar_tabela(tabela(v1));o1=processar(geo,bruto())
    v2=rede(date(2026,11,19),None);importar_tabela(tabela(v2,'ov-2'))
    o2=processar(geo,bruto(coleta=AGORA+timedelta(days=1)))
    assert (o1.versao_gtfs_id,o1.versao_correspondencia)==(v1.id,'ov-1')
    assert (o2.versao_gtfs_id,o2.versao_correspondencia)==(v2.id,'ov-2')
    assert o1.linha_codigo==o2.linha_codigo=='1012-10'

@pytest.mark.parametrize('y,idade,valida,motivo',[
    (100,300,True,'ASSOCIADA'),(100.01,300,False,'DISTANCIA_MAIOR_100M'),
    (99.99,300.001,False,'GPS_MAIS_5_MIN'),(0,-0.001,False,'GPS_POSTERIOR_COLETA'),
    (0,0,True,'ASSOCIADA'),(2100,30,False,'FORA_MUNICIPIO')])
def test_fronteiras(contexto,geo,y,idade,valida,motivo):
    v=rede();importar_tabela(tabela(v));b=bruto(y=y,idade=idade);original=deepcopy(b.payload)
    o=processar(geo,b)
    assert o.valida is valida and o.motivo_correspondencia==motivo
    assert b.payload==original and RegistroBrutoGPS.query.count()==1
    if valida:assert o.distancia_tracado_m<=100.000001

def test_garagem_preservada_no_bruto(contexto,geo):
    v=rede();importar_tabela(tabela(v));b=bruto(y=800);o=processar(geo,b)
    assert o.valida is False and db.session.get(RegistroBrutoGPS,b.id).payload is not None

def test_ambiguidades_nao_escolhem_primeira(contexto,geo):
    v=rede();importar_tabela(tabela(v));importar_tabela(tabela(v,'ov-2'))
    o=processar(geo,bruto())
    assert o.valida is True and o.linha_codigo is None
    assert o.motivo_correspondencia=='CORRESPONDENCIA_AMBIGUA' and len(o.candidatos_correspondencia)==2
    rede(date(2026,11,10),None);o2=processar(geo,bruto())
    assert o2.valida is None and o2.motivo_correspondencia=='GTFS_AMBIGUO'

def test_candidato_sem_geometria_fica_pendente(contexto,geo):
    v=rede();entradas=tabela(v)['entradas'];entradas.append({**entradas[0],'sentido_gtfs':'1'})
    importar_tabela(tabela(v,entradas=entradas));o=processar(geo,bruto())
    assert o.valida is None and o.motivo_correspondencia=='SEM_TRACADO_CANDIDATO'

def test_valida_sem_associacao_e_sentido_nao_equivalente(contexto,geo):
    v=rede();importar_tabela(tabela(v))
    for b in [bruto(codigo='desconhecido'),bruto(sentido='2')]:
        o=processar(geo,b)
        assert o.valida is True and o.linha_codigo is None and o.motivo_correspondencia=='CODIGO_SEM_CORRESPONDENCIA'

def test_sem_gtfs_nao_conta_como_invalida(contexto,geo):
    o=processar(geo,bruto());r=relatorio_diario(AGORA.date())
    assert o.valida is None and o.motivo_invalidez is None
    assert r['denominador']==r['invalidas']==0 and r['pendentes_validacao']==1
    assert r['taxa_associacao_pct'] is None and r['meta_atingida'] is None

def test_reprocessa_sem_duplicar_tratado(contexto,geo):
    v=rede();b=bruto();o=processar(geo,b);id=o.id
    assert o.valida and o.linha_codigo is None
    importar_tabela(tabela(v));o=processar(geo,b)
    assert o.id==id and o.linha_codigo=='1012-10' and ObservacaoTratada.query.count()==1

def test_fk_linha_versionada(contexto):
    v1=rede();rede(date(2026,11,19),None,codigo='OUTRA');b=bruto()
    db.session.add(ObservacaoTratada(bruto_id=b.id,valida=True,versao_correspondencia='v1',versao_gtfs_id=v1.id,linha_codigo='OUTRA'))
    with pytest.raises(IntegrityError):db.session.commit()
    db.session.rollback()

@pytest.mark.parametrize('query',['','?data=invalida','?data=2026-02-30','?data=2026-11-18&amostra=101','?data=2026-11-18&amostra=-1'])
def test_rota_filtro_invalido(contexto,query):
    assert contexto.test_client().get('/qualidade/correspondencias'+query).status_code==400

def test_relatorio_98_so_validas_e_troca_gtfs(contexto,geo):
    v=rede();importar_tabela(tabela(v));service=MatchingService(geo)
    for i in range(100):service.processar(bruto(codigo='1273' if i<98 else 'sem-vinculo').id)
    service.processar(bruto(y=800).id);bruto();db.session.commit()
    r=contexto.test_client().get(URL).get_json()
    assert (r['total_brutos'],r['numerador'],r['denominador'])==(102,98,100)
    assert r['meta_atingida'] and r['taxa_associacao_pct']==98
    assert r['invalidas']==1 and r['nao_processadas']==1 and len(r['amostra_nao_associados'])==2
    assert all(a['bruto_id'] and a['versao_correspondencia']=='ov-1' for a in r['amostra_nao_associados'])
    v2=rede(date(2026,11,19),None);importar_tabela(tabela(v2,'ov-2'))
    processar(geo,bruto(coleta=AGORA+timedelta(days=1)))
    r2=relatorio_diario(date(2026,11,19))
    assert r2['numerador']==1 and r2['grupos'][0]['versao_gtfs_id']==v2.id
    assert relatorio_diario(AGORA.date())['numerador']==98
    assert contexto.test_client().get('/api'+URL).status_code==200

def test_limite_dia_local(contexto,geo):
    rede()
    processar(geo,bruto(coleta=datetime(2026,11,19,2,59,59,tzinfo=timezone.utc)))
    processar(geo,bruto(coleta=datetime(2026,11,19,3,0,0,tzinfo=timezone.utc)))
    assert relatorio_diario(date(2026,11,18))['total_brutos']==1
    assert relatorio_diario(date(2026,11,19))['total_brutos']==1


def test_tempo_e_coordenadas_ausentes_sao_invalidos(contexto,geo):
    b=bruto();b.horario_gps=None;db.session.commit()
    assert processar(geo,b).motivo_invalidez=='HORARIO_AUSENTE'
    b=bruto();b.latitude=None;db.session.commit()
    assert processar(geo,b).motivo_invalidez=='COORDENADA_AUSENTE_OU_INVALIDA'


def test_troca_da_tabela_na_mesma_versao_gtfs_pela_vigencia(contexto,geo):
    v=rede(fim=None)
    d=tabela(v);d['vigencia_fim']='2026-11-18';importar_tabela(d)
    d=tabela(v,'ov-2');d['vigencia_inicio']='2026-11-19';importar_tabela(d)
    assert processar(geo,bruto()).versao_correspondencia=='ov-1'
    assert processar(geo,bruto(coleta=AGORA+timedelta(days=1))).versao_correspondencia=='ov-2'


def test_candidatos_conhecidos_ambiguos_na_mesma_tabela(contexto,geo):
    v=rede()
    db.session.add(Trecho(versao_gtfs_id=v.id,linha_codigo='1012-10',sentido='1',trajeto='viagem2',ordem=1,
        parada_origem_codigo='A',parada_destino_codigo='B',geometria=[xy(500,0),xy(-500,0)]))
    db.session.commit()
    entradas=tabela(v)['entradas'];entradas.append({**entradas[0],'sentido_gtfs':'1'})
    importar_tabela(tabela(v,entradas=entradas));o=processar(geo,bruto())
    assert o.valida is True and o.linha_codigo is None and o.motivo_correspondencia=='CORRESPONDENCIA_AMBIGUA'


def test_fk_correspondencia_nao_permite_outra_versao_gtfs(contexto):
    v1=rede();v2=rede(date(2026,11,19),None)
    importar_tabela(tabela(v1));c=CorrespondenciaCodigo.query.one();b=bruto()
    db.session.add(ObservacaoTratada(bruto_id=b.id,valida=True,versao_correspondencia='ov-1',
        versao_gtfs_id=v2.id,correspondencia_id=c.id))
    with pytest.raises(IntegrityError):db.session.commit()
    db.session.rollback()


def test_arredondamento_da_taxa_nao_aprova_meta(contexto):
    class Repo:
        def grupos_diarios(self,*args):
            return [(True,'linha','gtfs','v1','regra','ASSOCIADA',None,True,19599),
                    (True,None,'gtfs','v1','regra','SEM_VINCULO',None,True,401)]
        def amostra(self,*args):return []
    r=relatorio_diario(AGORA.date(),repository=Repo())
    assert r['taxa_associacao_pct']==98.0 and r['meta_atingida'] is False


def test_municipio_respeita_borda_e_buracos():
    anel=[[-47,-24],[-46,-24],[-46,-23],[-47,-23],[-47,-24]]
    buraco=[[-46.8,-23.8],[-46.6,-23.8],[-46.6,-23.6],[-46.8,-23.6],[-46.8,-23.8]]
    g=Geografia(json.dumps({'type':'Polygon','coordinates':[anel,buraco]}).encode())
    assert g.municipio(-47,-23.5) # borda incluída
    assert not g.municipio(-46.7,-23.7) # buraco não é município
    assert not g.municipio(-45.9,-23.5)


def test_importacao_invalida_nao_grava_vinculos_parciais(contexto):
    from app.models import TabelaCorrespondencia
    v=rede();d=tabela(v)
    d['entradas'].append({**d['entradas'][0],'linha_codigo':'NAO_EXISTE'})
    with pytest.raises(ValueError,match='linha inexistente'):importar_tabela(d)
    assert TabelaCorrespondencia.query.count()==CorrespondenciaCodigo.query.count()==0
    d=tabela(v);d['vigencia_fim']='2026-12-31'
    with pytest.raises(ValueError,match='contida'):importar_tabela(d)


def test_vinculo_sem_destino_tem_motivo_e_fica_no_relatorio(contexto,geo):
    v=rede();importar_tabela(tabela(v,entradas=[dict(codigo_olho_vivo='1273',
        sentido_olho_vivo='1',motivo='conferência pendente')]))
    b=bruto();o=processar(geo,b)
    assert o.valida is True and o.motivo_correspondencia=='VINCULO_NAO_RESOLVIDO'
    r=relatorio_diario(AGORA.date())
    assert r['denominador']==1 and r['numerador']==0 and not r['meta_atingida']
    assert r['amostra_nao_associados'][0]['candidatos'][0]['motivo']=='conferência pendente'


def test_lote_reserva_preserva_e_reprocessa_sem_duplicar(contexto,geo):
    from app.correspondencia.repository import CorrespondenciaLoteRepository
    v=rede();importar_tabela(tabela(v))
    b1=bruto();b2=bruto(codigo='desconhecido')
    ids=[b1.id,b2.id]
    antes={b.id:deepcopy(b.payload) for b in [b1,b2]}
    repo=CorrespondenciaLoteRepository();service=MatchingService(geo,repo)
    for _ in range(2):
        repo.preparar(ids)
        with db.session.no_autoflush:
            for id in ids:service.processar(id)
        repo.confirmar()
        db.session.commit()
    assert ObservacaoTratada.query.count()==2
    assert relatorio_diario(AGORA.date())['taxa_associacao_pct']==50
    assert {b.id:b.payload for b in RegistroBrutoGPS.query.all()}==antes


@pytest.mark.parametrize('sl,destino,esperado',[(1,'Destino A','1'),(2,'Destino B','0')])
def test_conferencia_destino_nao_subtrai_um_do_sentido(sl,destino,esperado):
    from app.correspondencia.fontes import conferir_vinculos
    entradas,_=conferir_vinculos({'l':[dict(c='X-10',cl=7,sl=sl,lt0='DESTINO Á',lt1='Destino B')]},
        [dict(route_id='route',route_short_name='X-10')],
        [dict(route_id='route',direction_id='1',trip_headsign='Destino A'),
         dict(route_id='route',direction_id='0',trip_headsign='Destino B')])
    assert entradas[0]['sentido_gtfs']==esperado


def test_conferencia_nao_inventa_sentido_ausente_nem_faz_fuzzy():
    from app.correspondencia.fontes import conferir_vinculos
    entradas,_=conferir_vinculos({'l':[dict(c='X-10',cl=7,sl=2,lt0='A',lt1='Terminal B')]},
        [dict(route_id='X',route_short_name='X-10')],
        [dict(route_id='X',direction_id='0',trip_headsign='Term. B')])
    assert entradas[0]['linha_codigo'] is None
    assert entradas[0]['motivo']=='DESTINO_NAO_ENCONTRADO_GTFS'


def test_conferencia_destino_ambiguo_preservado():
    from app.correspondencia.fontes import conferir_vinculos
    entradas,evidencias=conferir_vinculos({'l':[dict(c='X-10',cl=7,sl=1,lt0='A',lt1='B')]},
        [dict(route_id='X',route_short_name='X-10')],
        [dict(route_id='X',direction_id=s,trip_headsign='A') for s in ['0','1']])
    assert entradas[0]['motivo']=='DESTINO_AMBIGUO_GTFS'
    assert len(evidencias[0]['candidatos_gtfs'])==2


def circuito_fixture(abertura=0):
    from app.correspondencia.fontes import conferir_circuitos_gtfs
    trips=[dict(route_id='X',direction_id='0',trip_headsign='A',shape_id='S')]
    coords=[xy(0,0),xy(500,500),xy(0,500),xy(abertura,0)]
    pontos=[dict(shape_id='S',shape_pt_sequence=str(i),shape_pt_lon=str(p[0]),shape_pt_lat=str(p[1]))
            for i,p in enumerate(coords)]
    cad=[dict(cl=7,sl=2,lc=True,lt='X',tl=10,tp='A',ts='B')]
    return trips,conferir_circuitos_gtfs(trips,pontos),cad


def test_circular_conferido_usa_circuito_publicado_sem_inventar_sentido():
    from app.correspondencia.fontes import conferir_vinculos
    trips,circuitos,cad=circuito_fixture(abertura=20)
    entradas,e=conferir_vinculos({'l':[dict(c='X-10',cl=7,sl=2,lt0='A',lt1='B')]},
        [dict(route_id='X',route_short_name='X-10')],trips,cadastro=cad,circuitos=circuitos)
    assert entradas[0]['sentido_gtfs']=='0' and entradas[0]['linha_codigo']=='X'
    assert entradas[0]['motivo']=='CIRCUITO_UNICO_GTFS_CONFIRMADO'
    assert e[0]['circuito_conferido']['maior_abertura_m']==pytest.approx(20)


@pytest.mark.parametrize('abertura,aceito',[(0,True),(100,True),(100.01,False)])
def test_fechamento_do_circuito_em_metros(abertura,aceito):
    _,circuitos,_=circuito_fixture(abertura=abertura)
    assert ('X' in circuitos) is aceito


@pytest.mark.parametrize('falha',['sequencia_duplicada','sequencia_negativa','projecao_infinita'])
def test_circuito_rejeita_shape_com_sequencia_ou_projecao_invalida(falha):
    from app.correspondencia.fontes import conferir_circuitos_gtfs
    trips=[dict(route_id='X',direction_id='0',trip_headsign='A',shape_id='S')]
    pontos=[dict(shape_id='S',shape_pt_sequence=str(i),shape_pt_lon=str(p[0]),shape_pt_lat=str(p[1]))
        for i,p in enumerate([xy(0,0),xy(500,500),xy(0,500),xy(0,0)])]
    if falha=='sequencia_duplicada':pontos[1]['shape_pt_sequence']='0'
    if falha=='sequencia_negativa':pontos[1]['shape_pt_sequence']='-1'
    if falha=='projecao_infinita':pontos[1].update(shape_pt_lon='-135',shape_pt_lat='0')
    with pytest.raises(ValueError):conferir_circuitos_gtfs(trips,pontos)


def test_distancia_nao_mensuravel_nao_entra_na_taxa(contexto,geo,monkeypatch):
    from app.correspondencia.geografia import RedeEspacial
    v=rede();importar_tabela(tabela(v))
    monkeypatch.setattr(RedeEspacial,'distancia',lambda *args:float('nan'))
    o=processar(geo,bruto())
    assert o.valida is None and o.linha_codigo is None
    assert o.distancia_tracado_m is None
    assert o.motivo_correspondencia=='TRACADO_NAO_MENSURAVEL'
    assert relatorio_diario(AGORA.date())['denominador']==0


def test_relatorio_identifica_a_linha_de_cada_grupo(contexto,geo):
    v=rede();importar_tabela(tabela(v));processar(geo,bruto())
    assert relatorio_diario(AGORA.date())['grupos'][0]['linha_codigo']=='1012-10'


def test_rede_nao_usa_segmento_que_projeta_para_infinito():
    from types import SimpleNamespace
    from app.correspondencia.geografia import RedeEspacial
    trecho=SimpleNamespace(linha_codigo='X',sentido='0',geometria=[[-135,0],[-134,0]])
    rede_espacial=RedeEspacial([trecho])
    assert rede_espacial.distancia(Geografia.ponto(-46.63,-23.55)) is None


@pytest.mark.parametrize('falha',['aberto','nao_circular','cadastro_divergente','sem_cadastro','duas_direcoes','destino_divergente'])
def test_circular_nao_e_resolvido_por_suposta_equivalencia(falha):
    from app.correspondencia.fontes import conferir_vinculos
    trips,circuitos,cad=circuito_fixture(abertura=101 if falha=='aberto' else 20)
    if falha=='nao_circular':cad[0]['lc']=False
    if falha=='cadastro_divergente':cad[0]['tp']='Outro terminal'
    if falha=='sem_cadastro':cad=[]
    if falha=='duas_direcoes':trips.append(dict(route_id='X',direction_id='1',trip_headsign='C',shape_id='S'))
    if falha=='destino_divergente':trips[0]['trip_headsign']='Outro destino'
    entradas,_=conferir_vinculos({'l':[dict(c='X-10',cl=7,sl=2,lt0='A',lt1='B')]},
        [dict(route_id='X',route_short_name='X-10')],trips,cadastro=cad,circuitos=circuitos)
    assert entradas[0]['linha_codigo'] is None


def test_revisao_explicita_preserva_tabela_e_resolve_por_data(contexto,geo):
    from app.models import TabelaCorrespondencia
    v=rede();a=importar_tabela(tabela(v));hash_a=a.hash
    b=bruto();o=processar(geo,b);id_o=o.id
    nova={**tabela(v,'ov-revisada'), 'substitui_versao':a.versao,'vigencia_inicio':'2026-11-18'}
    revisada=importar_tabela(nova)
    assert importar_tabela(nova).id==revisada.id
    o=processar(geo,b)
    assert o.id==id_o and o.versao_correspondencia=='ov-revisada' and o.valida
    antigo=processar(geo,bruto(coleta=AGORA-timedelta(days=1)))
    assert antigo.versao_correspondencia=='ov-1'
    assert db.session.get(TabelaCorrespondencia,a.id).hash==hash_a
    assert CorrespondenciaCodigo.query.filter_by(tabela_id=a.id).count()==1


def test_forks_de_revisao_continuam_ambiguos(contexto,geo):
    v=rede();a=importar_tabela(tabela(v))
    for nome in ['revisao-a','revisao-b']:
        importar_tabela({**tabela(v,nome),'substitui_versao':a.versao})
    assert processar(geo,bruto()).motivo_correspondencia=='CORRESPONDENCIA_AMBIGUA'


def test_revisao_nao_aceita_gtfs_alheio_ou_vigencia_fora(contexto):
    v=rede();a=importar_tabela(tabela(v))
    outro=rede(date(2026,11,19),None)
    with pytest.raises(ValueError,match='mesma versão'):
        importar_tabela({**tabela(outro,'rev-outro'),'substitui_versao':a.versao})
    with pytest.raises(ValueError,match='si mesma'):
        importar_tabela({**tabela(v),'substitui_versao':a.versao})
    curta=importar_tabela({**tabela(v,'curta'),'vigencia_inicio':'2026-11-10'})
    with pytest.raises(ValueError,match='tabela anterior'):
        importar_tabela({**tabela(v,'rev-curta'),'substitui_versao':curta.versao})


def test_integracao_mysql_espacial_e_migracao(geo):
    import os
    from pathlib import Path
    from sqlalchemy.engine import make_url
    from sqlalchemy import inspect
    from flask_migrate import upgrade
    valor=os.getenv('US03_MYSQL_TEST_URL')
    if not valor:pytest.skip('US03_MYSQL_TEST_URL em base local descartável')
    url=make_url(valor)
    assert url.get_backend_name()=='mysql' and url.database.startswith('us03_verificacao_')
    app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':url,'MYSQL_SSL_CA':None})
    with app.app_context():
        try:
            assert not inspect(db.engine).get_table_names()
            upgrade(directory=str(Path(__file__).resolve().parents[1]/'migrations'))
            v=rede();importar_tabela(tabela(v))
            b=bruto(y=100,idade=300);o=processar(geo,b)
            assert o.valida and o.linha_codigo=='1012-10' and abs(o.distancia_tracado_m-100)<1e-6
            b2=bruto(codigo='sem-vinculo');assert processar(geo,b2).valida
            r=app.test_client().get(URL).get_json()
            assert r['numerador']==1 and r['denominador']==2 and r['taxa_associacao_pct']==50
            assert r['amostra_nao_associados'][0]['bruto_id']==b2.id
            assert db.session.get(RegistroBrutoGPS,b.id).payload['original']
            # Repetição na mesma tabela/versionamento não duplica os metadados.
            importar_tabela(tabela(v));assert CorrespondenciaCodigo.query.count()==1
            processar(geo,b);assert ObservacaoTratada.query.count()==2
            # A revisão não altera a tabela antiga nem duplica observações no upsert MySQL.
            anterior=importar_tabela(tabela(v))
            antiga_hash=anterior.hash
            revisada=importar_tabela({**tabela(v,'ov-revisada'),'substitui_versao':anterior.versao})
            from app.correspondencia.repository import CorrespondenciaLoteRepository
            repo=CorrespondenciaLoteRepository();repo.preparar([b.id,b2.id])
            service=MatchingService(geo,repo)
            with db.session.no_autoflush:
                service.processar(b.id);service.processar(b2.id)
            repo.confirmar();db.session.commit()
            assert ObservacaoTratada.query.count()==2 and anterior.hash==antiga_hash
            assert db.session.get(ObservacaoTratada,o.id).versao_correspondencia==revisada.versao
            # Worker B inicia um snapshot sem tratado. Worker A confirma o
            # tratamento; B precisa enxergar essa inserção após reservar o bruto.
            b3=bruto();id_b3=b3.id
            with app.app_context():
                assert ObservacaoTratada.query.filter_by(bruto_id=id_b3).one_or_none() is None
                with app.app_context():
                    id_confirmado=processar(geo,db.session.get(RegistroBrutoGPS,id_b3)).id
                from app.correspondencia.repository import CorrespondenciaRepository
                direto=CorrespondenciaRepository();direto.bruto_reservado(id_b3)
                assert direto.observacao(id_b3).id==id_confirmado
                lote=CorrespondenciaLoteRepository();lote.preparar([id_b3])
                assert lote.observacao(id_b3).id==id_confirmado
            # MySQL não aceita NaN como parâmetro numérico: a pendência grava NULL.
            from unittest.mock import patch
            b4=bruto()
            with patch('app.correspondencia.geografia.RedeEspacial.distancia',return_value=float('nan')):
                pendente=processar(geo,b4)
            assert pendente.valida is None and pendente.distancia_tracado_m is None
        finally:
            db.session.remove();db.engine.dispose()


def test_upgrade_mysql_da_main_preserva_programacao_e_integra_us03(geo):
    import os
    from sqlalchemy.engine import make_url
    from sqlalchemy import inspect
    from flask_migrate import upgrade
    from app.models import ImportacaoProgramacao,PartidaProgramada
    valor=os.getenv('US03_MAIN_MYSQL_TEST_URL')
    if not valor:pytest.skip('US03_MAIN_MYSQL_TEST_URL em base local descartável')
    url=make_url(valor)
    assert url.get_backend_name()=='mysql' and url.database.startswith('us03_verificacao_')
    app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':url,'MYSQL_SSL_CA':None})
    with app.app_context():
        try:
            assert not inspect(db.engine).get_table_names()
            upgrade(revision='0008')  # revisão da US04 já publicada na main
            v=rede()
            digest=uuid4().hex*2
            arquivo=ArquivoArmazenado(chave=digest,mime_type='application/json',nome_original='oficial.json',
                tamanho_bytes=2,sha256=digest)
            db.session.add(arquivo);db.session.flush()
            imp=ImportacaoProgramacao(versao_gtfs_id=v.id,linha_codigo='1012-10',data_operacao=AGORA.date(),
                sentido='0',tipo_dia='UTIL',fonte='OFICIAL',versao_fonte=digest,fonte_url='https://fonte.test',
                capturada_em=AGORA,verificada_em=AGORA,arquivo_id=arquivo.id)
            db.session.add(imp);db.session.flush()
            partida=PartidaProgramada(importacao_id=imp.id,ordem=1,saida_prevista=AGORA,tipo_oferta='FIXA')
            db.session.add(partida);db.session.commit();id_partida=partida.id;id_versao=v.id
            db.session.remove()  # encerra o snapshot anterior ao DDL, como no reinício do app
            upgrade()  # aplica US03 e a junção, sem reaplicar a US04
            v=db.session.get(VersaoGTFS,id_versao)
            assert db.session.get(PartidaProgramada,id_partida).saida_prevista==AGORA
            assert ImportacaoProgramacao.query.one().versao_fonte==digest
            importar_tabela(tabela(v))
            assert processar(geo,bruto()).valida
            assert PartidaProgramada.query.count()==1 and ObservacaoTratada.query.count()==1
        finally:db.session.remove();db.engine.dispose()
