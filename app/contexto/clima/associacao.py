"""ContextService — vínculo de cada trecho ao posto mais próximo (US06-T03).

Distância = menor distância entre o traçado do trecho (LineString) e o ponto
da estação, em metros, em SIRGAS 2000 / UTM 23S (EPSG:31983), o mesmo sistema
métrico da associação territorial (US24). Empate: menor código da estação.

Todo trecho da versão GTFS recebe uma linha em `trechos_estacoes_clima`:
- ASSOCIADO: estação mais próxima dentro do raio máximo;
- FORA_DO_RAIO: a mais próxima está além do raio; fica registrada, mas a
  consulta não usa a medição (bloqueio de cobertura explícito);
- SEM_ESTACAO: a fonte não tem estação cadastrada com coordenadas válidas;
- GEOMETRIA_INVALIDA: o traçado do trecho tem menos de dois pontos distintos ou
  coordenadas fora de WGS84; não há distância a medir (impedimento explícito).
"""

import math
from dataclasses import asdict, dataclass

from flask import current_app
from pyproj import Transformer
from shapely import transform
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from app import db
from app.models import (
    EstacaoMeteorologica,
    Trecho,
    TrechoEstacaoClima,
    VersaoGTFS,
    agora_utc,
)

CRS_MEDICAO = "EPSG:31983"
VERSAO_REGRA = "us06-t03-v2"


class VersaoGTFSNaoEncontrada(LookupError):
    pass


def _coordenada_valida(lon, lat):
    return (isinstance(lon, (int, float)) and isinstance(lat, (int, float))
            and math.isfinite(lon) and math.isfinite(lat)
            and -180 <= lon <= 180 and -90 <= lat <= 90 and (lon, lat) != (0, 0))


def _linha_metros(geometria, para_metros):
    """Traçado em metros, ou None se não houver dois pontos distintos válidos."""
    try:
        pontos = [(float(lon), float(lat)) for lon, lat in geometria or []]
    except (TypeError, ValueError):
        return None
    if len(set(pontos)) < 2 or not all(_coordenada_valida(*p) for p in pontos):
        return None
    return transform(LineString(pontos), para_metros.transform, interleaved=False)


@dataclass(frozen=True)
class ResultadoVinculoClima:
    fonte: str
    versao_gtfs_id: str
    raio_maximo_m: float
    trechos: int
    associados: int
    fora_do_raio: int
    sem_estacao: int
    geometria_invalida: int
    estacoes_usadas: int

    @property
    def identificados_pct(self):
        """Trechos com posto identificado ou bloqueio explícito (meta: 100%)."""
        resolvidos = self.associados + self.fora_do_raio + self.sem_estacao + self.geometria_invalida
        return 100.0 if self.trechos == 0 else round(100 * resolvidos / self.trechos, 2)

    def como_dict(self):
        return {**asdict(self), "identificados_pct": self.identificados_pct}


def associar_trechos_a_estacoes(versao_gtfs_id, fonte="INMET", raio_maximo_m=None, agora=None):
    """Recalcula, numa única transação, os vínculos de todos os trechos da versão."""
    if db.session.get(VersaoGTFS, versao_gtfs_id) is None:
        raise VersaoGTFSNaoEncontrada(versao_gtfs_id)
    raio = float(raio_maximo_m if raio_maximo_m is not None
                 else current_app.config["CLIMA_RAIO_MAXIMO_M"])
    agora = agora or agora_utc()
    para_metros = Transformer.from_crs("EPSG:4326", CRS_MEDICAO, always_xy=True)

    estacoes = [e for e in EstacaoMeteorologica.query.filter_by(fonte=fonte)
                .order_by(EstacaoMeteorologica.codigo)
                if _coordenada_valida(float(e.longitude), float(e.latitude))]
    pontos = [transform(Point(float(e.longitude), float(e.latitude)), para_metros.transform,
                        interleaved=False) for e in estacoes]
    arvore = STRtree(pontos) if pontos else None

    TrechoEstacaoClima.query.filter_by(fonte=fonte, versao_gtfs_id=versao_gtfs_id).delete()
    contagem = {"ASSOCIADO": 0, "FORA_DO_RAIO": 0, "SEM_ESTACAO": 0, "GEOMETRIA_INVALIDA": 0}
    usadas = set()
    trechos = Trecho.query.filter_by(versao_gtfs_id=versao_gtfs_id).order_by(Trecho.id).all()
    for trecho in trechos:
        estacao, distancia, situacao = None, None, "SEM_ESTACAO"
        linha = _linha_metros(trecho.geometria, para_metros)
        if linha is None:
            situacao = "GEOMETRIA_INVALIDA"
        elif arvore is not None:
            indice_mais_proximo = int(arvore.nearest(linha))
            menor = pontos[indice_mais_proximo].distance(linha)
            # Desempate determinístico entre estações à mesma distância.
            empatadas = [i for i, p in enumerate(pontos) if abs(p.distance(linha) - menor) <= 1e-6]
            indice = min(empatadas, key=lambda i: estacoes[i].codigo)
            estacao, distancia = estacoes[indice], round(menor, 3)
            situacao = "ASSOCIADO" if distancia <= raio else "FORA_DO_RAIO"
            if situacao == "ASSOCIADO":
                usadas.add(estacao.id)
        contagem[situacao] += 1
        db.session.add(TrechoEstacaoClima(
            trecho_id=trecho.id, fonte=fonte, versao_gtfs_id=trecho.versao_gtfs_id,
            linha_codigo=trecho.linha_codigo, estacao_id=estacao.id if estacao else None,
            distancia_m=distancia, raio_maximo_m=raio, situacao=situacao,
            crs_medicao=CRS_MEDICAO, versao_regra=VERSAO_REGRA, calculado_em=agora,
        ))
    db.session.commit()
    return ResultadoVinculoClima(
        fonte=fonte, versao_gtfs_id=versao_gtfs_id, raio_maximo_m=raio, trechos=len(trechos),
        associados=contagem["ASSOCIADO"], fora_do_raio=contagem["FORA_DO_RAIO"],
        sem_estacao=contagem["SEM_ESTACAO"], geometria_invalida=contagem["GEOMETRIA_INVALIDA"],
        estacoes_usadas=len(usadas),
    )
