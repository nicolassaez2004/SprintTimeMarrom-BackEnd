"""Operações internas fora das requisições HTTP (US03)."""
import argparse
import json
from datetime import date
from pathlib import Path
import requests
import csv
import hashlib
from app import create_app,db
from app.models import RegistroBrutoGPS,ObservacaoTratada
from .tabelas import importar_tabela
from .geografia import Geografia
from .service import MatchingService
from .relatorio import limites_dia,relatorio_diario
from .repository import CorrespondenciaLoteRepository
from .fontes import conferir_vinculos,conferir_circuitos_gtfs

LIMITE_URL='https://servicodados.ibge.gov.br/api/v3/malhas/municipios/3550308?formato=application/vnd.geo+json&qualidade=maxima'


def baixar_limite(caminho):
    resposta=requests.get(LIMITE_URL,timeout=(10,90))
    resposta.raise_for_status()
    geo=Geografia(resposta.content)  # valida antes da publicação
    caminho=Path(caminho);caminho.parent.mkdir(parents=True,exist_ok=True)
    # Downloads idênticos são reaproveitados; preservar arquivo anterior diferente.
    if caminho.exists():
        if caminho.read_bytes()!=resposta.content:raise ValueError('Destino já existe com outra versão do limite; use novo caminho')
    else:
        with caminho.open('xb') as arquivo:arquivo.write(resposta.content)
    print(f'Limite municipal salvo em {caminho.resolve()} | {geo.versao}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='comando',required=True)
    imp=sub.add_parser('importar');imp.add_argument('arquivo')
    baixar=sub.add_parser('baixar-limite');baixar.add_argument('--destino',default='instance/municipio-3550308.geojson')
    linhas=sub.add_parser('consultar-linhas');linhas.add_argument('--termo',required=True)
    conferir=sub.add_parser('conferir-fontes',help='Gera uma tabela revisável, sem gravar vínculos no banco')
    conferir.add_argument('--captura',required=True);conferir.add_argument('--cadastro',required=True)
    conferir.add_argument('--versao-gtfs',required=True);conferir.add_argument('--versao',required=True)
    conferir.add_argument('--inicio',required=True,type=date.fromisoformat)
    conferir.add_argument('--substitui-versao');conferir.add_argument('--destino',required=True)
    proc=sub.add_parser('processar');proc.add_argument('--data',required=True,type=date.fromisoformat)
    proc.add_argument('--limite-municipal');proc.add_argument('--lote',type=int,default=500)
    proc.add_argument('--max-registros',type=int,default=10000);proc.add_argument('--reprocessar',action='store_true')
    rel=sub.add_parser('relatorio');rel.add_argument('--data',required=True,type=date.fromisoformat)
    args=parser.parse_args()
    if args.comando=='baixar-limite':baixar_limite(args.destino);return
    app=create_app()
    with app.app_context():
        try:
            if args.comando=='consultar-linhas':
                from app.integrations.sptrans import SPTransClient
                print(json.dumps(SPTransClient().buscar_linhas(args.termo),ensure_ascii=False,indent=2))
            elif args.comando=='importar':
                tabela=importar_tabela(json.loads(Path(args.arquivo).read_text(encoding='utf-8-sig')))
                print(f'Tabela {tabela.versao} | id={tabela.id} | hash={tabela.hash}')
            elif args.comando=='conferir-fontes':
                from app.gtfs.service import _abrir_versao
                captura=Path(args.captura).read_bytes();cadastro=Path(args.cadastro).read_bytes()
                resposta=json.loads(captura)['resposta'];linhas=json.loads(cadastro)['linhas']
                v,arquivo=_abrir_versao(args.versao_gtfs,None)
                with arquivo:
                    ler=lambda nome:csv.DictReader(l.decode('utf-8-sig') for l in arquivo.open(nome))
                    rotas=list(ler('routes.txt'));viagens=list(ler('trips.txt'))
                    circuitos=conferir_circuitos_gtfs(viagens,ler('shapes.txt'))
                entradas,evidencias=conferir_vinculos(resposta,rotas,viagens,cadastro=linhas,circuitos=circuitos)
                hashes=dict(captura=hashlib.sha256(captura).hexdigest(),
                            cadastro=hashlib.sha256(cadastro).hexdigest(),gtfs=v.hash)
                pacote=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
                dados=dict(versao=args.versao,versao_gtfs_id=v.id,vigencia_inicio=args.inicio.isoformat(),
                    vigencia_fim=v.vigencia_fim.isoformat() if v.vigencia_fim else None,
                    fonte='SPTrans Posicao/Linha + GTFS; conferencia v2; pacote SHA256 '+pacote,entradas=entradas)
                if args.substitui_versao:dados['substitui_versao']=args.substitui_versao
                destino=Path(args.destino);destino.parent.mkdir(parents=True,exist_ok=True)
                auditoria=destino.with_suffix('.evidencias.json')
                if destino.exists() or auditoria.exists():raise ValueError('Destino já existe; use outro nome')
                destino.write_text(json.dumps(dados,ensure_ascii=False,indent=2),encoding='utf-8')
                auditoria.write_text(json.dumps(dict(hashes=hashes,entradas=evidencias),ensure_ascii=False,indent=2),encoding='utf-8')
                print(f'Tabela revisável: {destino.resolve()} | evidências: {auditoria.resolve()}')
            elif args.comando=='relatorio':print(json.dumps(relatorio_diario(args.data),ensure_ascii=False,indent=2))
            else:
                caminho=args.limite_municipal or app.config.get('MUNICIPIO_GEOJSON')
                if not caminho:raise ValueError('Informe --limite-municipal ou MUNICIPIO_GEOJSON')
                if not 1<=args.lote<=1000 or not 1<=args.max_registros<=100000:raise ValueError('Lote 1..1000; máximo 1..100000')
                repo=CorrespondenciaLoteRepository()
                service=MatchingService(Geografia.arquivo(caminho),repository=repo)
                inicio,fim=limites_dia(args.data);ultimo=0;total=0
                while total<args.max_registros:
                    consulta=db.session.query(RegistroBrutoGPS.id).outerjoin(ObservacaoTratada,
                        ObservacaoTratada.bruto_id==RegistroBrutoGPS.id).filter(
                        RegistroBrutoGPS.id>ultimo,RegistroBrutoGPS.coletado_em>=inicio,
                        RegistroBrutoGPS.coletado_em<fim)
                    if not args.reprocessar:consulta=consulta.filter(ObservacaoTratada.processada_em.is_(None))
                    ids=[x[0] for x in consulta.order_by(RegistroBrutoGPS.id).limit(min(args.lote,args.max_registros-total)).all()]
                    if not ids:break
                    repo.preparar(ids)
                    with db.session.no_autoflush:
                        for id in ids:service.processar(id)
                    repo.confirmar()
                    db.session.commit();ultimo=ids[-1];total+=len(ids)
                    print(f'Lote confirmado: {total} observações.',flush=True)
                print(f'{total} observações processadas; brutos preservados. Consulte o relatório diário.')
        except Exception:
            db.session.rollback();raise
        finally:db.session.remove();db.engine.dispose()


if __name__=='__main__':main()
