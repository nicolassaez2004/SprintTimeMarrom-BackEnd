"""Coordenadas WGS84 [longitude, latitude]; distâncias em SIRGAS2000/UTM23S."""
import hashlib
import json
import math
from pathlib import Path
from pyproj import Transformer
from shapely.geometry import Point, LineString, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

PROJECAO = Transformer.from_crs('EPSG:4326','EPSG:31983',always_xy=True)


class Geografia:
    def __init__(self, geojson_bytes):
        data = json.loads(geojson_bytes)
        if data.get('crs'):
            nome = data['crs'].get('properties',{}).get('name','')
            if nome not in ('urn:ogc:def:crs:OGC:1.3:CRS84','EPSG:4326'):
                raise ValueError('Limite municipal precisa estar em WGS84/GeoJSON RFC7946')
        geoms = [shape(f['geometry']) for f in data['features']] if data['type']=='FeatureCollection' else [shape(data.get('geometry',data))]
        if not geoms or any(g.geom_type not in ('Polygon','MultiPolygon') or not g.is_valid or g.is_empty for g in geoms):
            raise ValueError('Limite municipal vazio ou inválido')
        self.limite = unary_union(geoms)
        x1,y1,x2,y2 = self.limite.bounds
        if not (-180 <= x1 <= x2 <= 180 and -90 <= y1 <= y2 <= 90):
            raise ValueError('Limite municipal fora de WGS84')
        self.versao = 'us03-v1/31983/'+hashlib.sha256(geojson_bytes).hexdigest()

    @classmethod
    def arquivo(cls, caminho):
        return cls(Path(caminho).read_bytes())

    def municipio(self, longitude, latitude):
        return self.limite.covers(Point(longitude,latitude))

    @staticmethod
    def ponto(longitude, latitude):
        return Point(*PROJECAO.transform(longitude,latitude))


class RedeEspacial:
    def __init__(self, trechos):
        self.geometrias = {}
        for trecho in trechos:
            coords = trecho.geometria
            if not coords or len(coords)<2 or any(not math.isfinite(float(n)) for xy in coords for n in xy):
                continue
            xs,ys=PROJECAO.transform(*zip(*coords))
            if any(not math.isfinite(n) for n in (*xs,*ys)):
                continue
            linha = LineString(zip(xs,ys))
            self.geometrias.setdefault((trecho.linha_codigo,trecho.sentido),[]).append(linha)
        self.indices = {k:STRtree(v) for k,v in self.geometrias.items()}
        todas = [g for gs in self.geometrias.values() for g in gs]
        self.todas = STRtree(todas) if todas else None

    def distancia(self, ponto, destinos=None):
        if destinos is None:
            if self.todas is None:return None
            i = self.todas.nearest(ponto)
            return float(ponto.distance(self.todas.geometries[i]))
        if not destinos or any(d not in self.indices for d in destinos):return None
        valores=[]
        for d in destinos:
            tree=self.indices[d]; i=tree.nearest(ponto)
            valores.append(float(ponto.distance(tree.geometries[i])))
        return min(valores)
