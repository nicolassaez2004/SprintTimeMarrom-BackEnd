"""MatchingService sem HTTP: resolve por data, valida e preserva origem."""
import hashlib
import math
from datetime import timezone
from zoneinfo import ZoneInfo
from app.gtfs.service import versao_vigente, VersaoGTFSInexistente, VersaoGTFSAmbigua
from app.models import ObservacaoTratada, agora_utc
from .repository import CorrespondenciaRepository
from .geografia import RedeEspacial

FUSO=ZoneInfo('America/Sao_Paulo')


class MatchingService:
    def __init__(self, geografia, repository=None):
        self.geo=geografia
        self.repo=repository or CorrespondenciaRepository()
        self.redes={}
        self.resolucoes={}
        self.versoes={}

    def processar(self, bruto_id):
        bruto=self.repo.bruto_reservado(bruto_id)
        obs=self.repo.observacao(bruto_id) or ObservacaoTratada(bruto_id=bruto_id)
        # Reprocessamento explícito atualiza somente o tratado. Nunca o bruto.
        obs.valida=None;obs.motivo_invalidez=None;obs.versao_gtfs_id=None
        obs.linha_codigo=None;obs.sentido_gtfs=None;obs.correspondencia_id=None
        obs.candidatos_correspondencia=[];obs.distancia_tracado_m=None
        obs.versao_correspondencia='SEM_TABELA';obs.versao_validacao=self.geo.versao
        obs.processada_em=agora_utc();obs.motivo_correspondencia=None
        def terminar(valida,motivo):
            obs.valida=valida
            obs.motivo_correspondencia=motivo
            if valida is False:obs.motivo_invalidez=motivo
            self.repo.guardar(obs)
            return obs
        coords=(bruto.longitude,bruto.latitude)
        if any(v is None or not math.isfinite(v) for v in coords):return terminar(False,'COORDENADA_AUSENTE_OU_INVALIDA')
        if not (-180<=coords[0]<=180 and -90<=coords[1]<=90):return terminar(False,'COORDENADA_FORA_FAIXA')
        gps=bruto.horario_gps; coleta=bruto.coletado_em
        if not gps or not coleta:return terminar(False,'HORARIO_AUSENTE')
        if gps.tzinfo is None or coleta.tzinfo is None:raise ValueError('horário sem fuso')
        idade=(coleta.astimezone(timezone.utc)-gps.astimezone(timezone.utc)).total_seconds()
        if idade<0:return terminar(False,'GPS_POSTERIOR_COLETA')
        if idade>300:return terminar(False,'GPS_MAIS_5_MIN')
        if not self.geo.municipio(*coords):return terminar(False,'FORA_MUNICIPIO')
        dia=gps.astimezone(FUSO).date()
        try:
            if dia not in self.versoes:self.versoes[dia]=versao_vigente(dia)
            v=self.versoes[dia]
        except VersaoGTFSInexistente:return terminar(None,'SEM_GTFS_VIGENTE')
        except VersaoGTFSAmbigua as erro:
            obs.candidatos_correspondencia=[{'versao_gtfs_id':x.id} for x in erro.versoes]
            return terminar(None,'GTFS_AMBIGUO')
        obs.versao_gtfs_id=v.id
        chave=(v.id,dia,bruto.linha_origem,bruto.sentido_origem)
        if chave not in self.resolucoes:
            tabelas=self.repo.tabelas(v.id,dia)
            candidatos=self.repo.candidatos(tabelas,bruto.linha_origem,bruto.sentido_origem)
            self.resolucoes[chave]=(tabelas,candidatos)
        tabelas,candidatos=self.resolucoes[chave]
        nomes=sorted(t.versao for t in tabelas)
        if len(nomes)==1:obs.versao_correspondencia=nomes[0]
        elif nomes:obs.versao_correspondencia='MULTIPLAS:'+hashlib.sha256('|'.join(nomes).encode()).hexdigest()
        obs.candidatos_correspondencia=[dict(id=c.id,tabela_id=c.tabela_id,
            versao=next(t.versao for t in tabelas if t.id==c.tabela_id),
            linha_codigo=c.linha_codigo,sentido_gtfs=c.sentido_gtfs,motivo=c.motivo) for c in candidatos]
        obs.candidatos_correspondencia += [dict(tabela_id=t.id,versao=t.versao,sem_entrada=True)
            for t in tabelas if not any(c.tabela_id==t.id for c in candidatos)]
        if v.id not in self.redes:self.redes[v.id]=RedeEspacial(self.repo.trechos(v.id))
        resolvidos=[c for c in candidatos if c.linha_codigo is not None]
        destinos={(c.linha_codigo,c.sentido_gtfs) for c in resolvidos} if resolvidos else None
        distancia=self.redes[v.id].distancia(self.geo.ponto(*coords),destinos)
        obs.distancia_tracado_m=distancia
        if distancia is None:return terminar(None,'SEM_TRACADO_CANDIDATO')
        if not math.isfinite(distancia):
            obs.distancia_tracado_m=None
            return terminar(None,'TRACADO_NAO_MENSURAVEL')
        if distancia>100+1e-7:return terminar(False,'DISTANCIA_MAIOR_100M')
        # Validade sem vínculo: ponto na rede vigente, mas código não resolvido.
        if not candidatos:return terminar(True,'CODIGO_SEM_CORRESPONDENCIA' if tabelas else 'SEM_TABELA_VIGENTE')
        if len(candidatos)>1 or len(tabelas)>1:return terminar(True,'CORRESPONDENCIA_AMBIGUA')
        c=candidatos[0]
        if c.linha_codigo is None:return terminar(True,'VINCULO_NAO_RESOLVIDO')
        obs.linha_codigo=c.linha_codigo;obs.sentido_gtfs=c.sentido_gtfs;obs.correspondencia_id=c.id
        return terminar(True,'ASSOCIADA')
