from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from .service import FUSO
from .repository import CorrespondenciaRepository


def limites_dia(dia):
    if type(dia) is not date:raise ValueError('data deve ser YYYY-MM-DD')
    inicio=datetime.combine(dia,time.min,tzinfo=FUSO)
    fim=datetime.combine(dia+timedelta(days=1),time.min,tzinfo=FUSO)
    return inicio.astimezone(timezone.utc),fim.astimezone(timezone.utc)


def relatorio_diario(dia, amostra=20, repository=None):
    if not 0<=amostra<=100:raise ValueError('amostra deve ficar entre 0 e 100')
    repo=repository or CorrespondenciaRepository()
    inicio,fim=limites_dia(dia)
    contadores=dict(total_brutos=0,validas=0,associadas=0,nao_associadas_validas=0,
                    invalidas=0,pendentes_validacao=0,nao_processadas=0)
    grupos=[]
    for valida,linha,gtfs,versao,regra,motivo,invalidez,processada,total in repo.grupos_diarios(inicio,fim):
        contadores['total_brutos']+=total
        if not processada:estado='NAO_PROCESSADA';contadores['nao_processadas']+=total
        elif valida is None:estado='PENDENTE';contadores['pendentes_validacao']+=total
        elif not valida:estado='INVALIDA';contadores['invalidas']+=total
        else:
            contadores['validas']+=total
            if linha is not None:estado='ASSOCIADA';contadores['associadas']+=total
            else:estado='SEM_ASSOCIACAO';contadores['nao_associadas_validas']+=total
        grupos.append(dict(estado=estado,linha_codigo=linha,versao_gtfs_id=gtfs,versao_correspondencia=versao,
            versao_validacao=regra,motivo=motivo or invalidez,quantidade=total))
    numerador=contadores['associadas'];denominador=contadores['validas']
    taxa=Decimal(numerador)*100/Decimal(denominador) if denominador else None
    amostras=[]
    for bruto,obs in repo.amostra(inicio,fim,amostra):
        amostras.append(dict(bruto_id=bruto.id,observacao_id=obs.id,execucao_id=bruto.execucao_id,
            codigo_olho_vivo=bruto.linha_origem,sentido_olho_vivo=bruto.sentido_origem,
            coletado_em=bruto.coletado_em.isoformat(),
            horario_gps=bruto.horario_gps.isoformat() if bruto.horario_gps else None,
            versao_gtfs_id=obs.versao_gtfs_id,versao_correspondencia=obs.versao_correspondencia,
            motivo=obs.motivo_correspondencia,candidatos=obs.candidatos_correspondencia))
    return dict(data=dia.isoformat(),fuso='America/Sao_Paulo',
        criterio_dia='coletado_em em [00:00, 00:00 do dia seguinte)',
        **contadores,numerador=numerador,denominador=denominador,
        taxa_associacao_pct=float(taxa.quantize(Decimal('0.01'))) if taxa is not None else None,
        meta_pct=98,meta_atingida=(taxa>=98) if taxa is not None else None,
        grupos=sorted(grupos,key=lambda g:tuple(str(g[k]) for k in ('estado','versao_gtfs_id','versao_correspondencia','linha_codigo','motivo','versao_validacao'))),
        amostra_nao_associados=amostras,limite_amostra=amostra)
