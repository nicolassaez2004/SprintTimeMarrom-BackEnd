"""Importação explícita, atômica e idempotente de vínculos conferidos."""
import hashlib
import json
from datetime import date
from uuid import uuid4
from sqlalchemy.exc import IntegrityError
from app import db
from app.models import TabelaCorrespondencia, CorrespondenciaCodigo, Linha, VersaoGTFS


def _texto(valor, campo, maximo):
    if not isinstance(valor,str) or not valor.strip() or len(valor)>maximo:
        raise ValueError(f'{campo}: texto obrigatório, até {maximo} caracteres')
    return valor


def importar_tabela(dados):
    versao=_texto(dados.get('versao'),'versao',96)
    gtfs_id=_texto(dados.get('versao_gtfs_id'),'versao_gtfs_id',36)
    fonte=_texto(dados.get('fonte'),'fonte',255)
    inicio=date.fromisoformat(dados['vigencia_inicio'])
    fim=date.fromisoformat(dados['vigencia_fim']) if dados.get('vigencia_fim') else None
    if fim and fim<inicio:raise ValueError('vigência invertida')
    gtfs=db.session.get(VersaoGTFS,gtfs_id)
    if not gtfs:raise ValueError('versão GTFS não cadastrada')
    if inicio<gtfs.vigencia_inicio or (gtfs.vigencia_fim and (fim is None or fim>gtfs.vigencia_fim)):
        raise ValueError('vigência da tabela deve estar contida na versão GTFS')
    anterior=None
    if dados.get('substitui_versao') is not None:
        nome_anterior=_texto(dados['substitui_versao'],'substitui_versao',96)
        anterior=TabelaCorrespondencia.query.filter_by(versao=nome_anterior).one_or_none()
        if not anterior or anterior.versao_gtfs_id!=gtfs_id:
            raise ValueError('tabela anterior deve existir na mesma versão GTFS')
        if nome_anterior==versao:raise ValueError('tabela não pode substituir a si mesma')
        if inicio<anterior.vigencia_inicio or (anterior.vigencia_fim and (fim is None or fim>anterior.vigencia_fim)):
            raise ValueError('vigência da revisão deve estar contida na tabela anterior')
    entradas=dados.get('entradas')
    if not isinstance(entradas,list) or not entradas:raise ValueError('entradas: lista não vazia obrigatória')
    linhas={codigo for (codigo,) in db.session.query(Linha.codigo).filter_by(versao_gtfs_id=gtfs_id)}
    normalizadas=[]
    for e in entradas:
        codigo=_texto(e.get('codigo_olho_vivo'),'codigo_olho_vivo',32)
        sentido=e.get('sentido_olho_vivo')
        if sentido not in ('1','2'):raise ValueError('sentido_olho_vivo deve ser texto 1 ou 2')
        linha=e.get('linha_codigo'); sgtfs=e.get('sentido_gtfs'); motivo=e.get('motivo')
        if linha is None:
            if sgtfs is not None:raise ValueError('destino sem linha')
            motivo=_texto(motivo,'motivo do vínculo não resolvido',255)
        else:
            linha=_texto(linha,'linha_codigo',64)
            if sgtfs not in ('0','1'):raise ValueError('sentido_gtfs deve ser texto 0 ou 1')
            if linha not in linhas:raise ValueError('linha inexistente nesta versão GTFS')
            if motivo is not None:motivo=_texto(motivo,'motivo',255)
        normalizadas.append(dict(codigo_olho_vivo=codigo,sentido_olho_vivo=sentido,
                                 linha_codigo=linha,sentido_gtfs=sgtfs,motivo=motivo))
    canonicas=sorted(json.dumps(e,sort_keys=True,ensure_ascii=False) for e in normalizadas)
    if len(canonicas)!=len(set(canonicas)):raise ValueError('entrada repetida na mesma tabela')
    componentes=[versao,gtfs_id,fonte,inicio.isoformat(),fim.isoformat() if fim else None,canonicas]
    if anterior:componentes.append({'substitui_id':anterior.id})
    conteudo=json.dumps(componentes,ensure_ascii=False)
    digest=hashlib.sha256(conteudo.encode()).hexdigest()
    existente=TabelaCorrespondencia.query.filter_by(versao=versao).one_or_none()
    if existente:
        if existente.hash!=digest:raise ValueError('versão já existe com outro conteúdo; crie nova versão')
        return existente
    tabela=TabelaCorrespondencia(versao=versao,versao_gtfs_id=gtfs_id,fonte=fonte,
                                vigencia_inicio=inicio,vigencia_fim=fim,hash=digest,
                                substitui_id=anterior.id if anterior else None)
    try:
        db.session.add(tabela); db.session.flush()
        for inicio_lote in range(0,len(normalizadas),250):
            registros=[dict(id=str(uuid4()),tabela_id=tabela.id,versao_gtfs_id=gtfs_id,**e)
                       for e in normalizadas[inicio_lote:inicio_lote+250]]
            db.session.execute(CorrespondenciaCodigo.__table__.insert().values(registros))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existente=TabelaCorrespondencia.query.filter_by(versao=versao).one_or_none()
        if existente and existente.hash==digest:return existente
        raise
    except Exception:
        db.session.rollback(); raise
    return tabela
