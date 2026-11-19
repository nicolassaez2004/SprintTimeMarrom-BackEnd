"""Conferência conservadora entre letreiro da frota e destino publicado no GTFS."""
import unicodedata
import math
from collections import defaultdict
from .geografia import PROJECAO


def normalizar_destino(texto):
    return ''.join(c for c in unicodedata.normalize('NFKD', texto or '').upper() if c.isalnum())


def conferir_circuitos_gtfs(viagens, pontos):
    """Circuito único publicado, com todos os shapes completos e fechados até 100 m.

    O cadastro circular da SPTrans ainda é exigido por conferir_vinculos.
    Esta função apenas confere a representação espacial da versão GTFS.
    """
    por_rota = defaultdict(list)
    for t in viagens:
        por_rota[t['route_id']].append(t)
    candidatas = {r: ts for r, ts in por_rota.items()
                  if len({t.get('direction_id') for t in ts}) == 1
                  and ts[0].get('direction_id') in ('0', '1')
                  and all(t.get('shape_id') for t in ts)}
    necessarios = {t['shape_id'] for ts in candidatas.values() for t in ts}
    extremos = {}
    for p in pontos:
        chave = p['shape_id']
        if chave not in necessarios:
            continue
        seq = int(p['shape_pt_sequence'])
        if seq < 0:
            raise ValueError('Sequência de shape negativa')
        lon, lat = float(p['shape_pt_lon']), float(p['shape_pt_lat'])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError('Shape fora da faixa WGS84')
        ponto = PROJECAO.transform(lon, lat)
        if not all(math.isfinite(n) for n in ponto):
            raise ValueError('Shape não pode ser projetado em UTM23S')
        if chave not in extremos:
            extremos[chave] = [seq, ponto, seq, ponto, set(), set()]
        e = extremos[chave]
        if seq in e[5]:
            raise ValueError('Sequência duplicada no mesmo shape')
        e[5].add(seq)
        if seq < e[0]:e[0], e[1] = seq, ponto
        if seq > e[2]:e[2], e[3] = seq, ponto
        if len(e[4]) < 3:e[4].add(ponto)
    resultado = {}
    for rota, ts in candidatas.items():
        shapes = sorted({t['shape_id'] for t in ts})
        if any(s not in extremos or len(extremos[s][4]) < 3 for s in shapes):
            continue
        distancias = [math.dist(extremos[s][1], extremos[s][3]) for s in shapes]
        if max(distancias) <= 100 + 1e-7:
            resultado[rota] = dict(sentido_gtfs=ts[0]['direction_id'],
                shapes=shapes, maior_abertura_m=max(distancias), tolerancia_m=100)
    return resultado


def conferir_vinculos(resposta, rotas, viagens, *, cadastro=(), circuitos=None):
    """Não usa proximidade, similaridade textual ou conversão aritmética de sentido.

    `lt0` é o letreiro para sl=1 e `lt1` para sl=2, segundo a API SPTrans.
    Exceção explicitamente conferida: fonte lc=True e GTFS com circuito único
    fechado. Os letreiros da Olho Vivo passam a representar partes desse mesmo
    circuito, mantendo o único direction_id realmente publicado pelo GTFS.
    """
    por_letreiro = defaultdict(list)
    por_rota = defaultdict(list)
    for rota in rotas:
        por_letreiro[rota['route_short_name']].append(rota)
    for viagem in viagens:
        por_rota[viagem['route_id']].append(viagem)
    catalogo = {}
    for r in cadastro:
        chave = (str(r['cl']), str(r['sl']))
        if chave in catalogo and catalogo[chave] != r:
            raise ValueError('Cadastro Olho Vivo conflitante')
        catalogo[chave] = r
    circuitos = circuitos or {}
    entradas, evidencias = [], []
    fontes = {}
    for linha in resposta['l']:
        chave = (str(linha['cl']), str(linha['sl']))
        if chave in fontes and fontes[chave] != {k: linha.get(k) for k in ('c', 'lt0', 'lt1')}:
            raise ValueError('Mesmo código/sentido com metadados conflitantes na fonte')
        if chave in fontes:
            continue
        fontes[chave] = {k: linha.get(k) for k in ('c', 'lt0', 'lt1')}
        if chave[1] not in ('1', '2'):
            raise ValueError('Sentido da fonte inválido')
        destino = linha.get('lt0' if chave[1] == '1' else 'lt1')
        candidatas = por_letreiro.get(linha['c'], [])
        opcoes = {(r['route_id'], t['direction_id']) for r in candidatas
                  for t in por_rota[r['route_id']]
                  if normalizar_destino(destino)
                  and normalizar_destino(t.get('trip_headsign')) == normalizar_destino(destino)
                  and t['direction_id'] in ('0', '1')}
        e = dict(codigo_olho_vivo=chave[0], sentido_olho_vivo=chave[1])
        if len(candidatas) == 1 and len(opcoes) == 1:
            e['linha_codigo'], e['sentido_gtfs'] = next(iter(opcoes))
        else:
            e.update(linha_codigo=None, sentido_gtfs=None, motivo=(
                'LETREIRO_NAO_ENCONTRADO_GTFS' if not candidatas else
                'LETREIRO_AMBIGUO_GTFS' if len(candidatas) > 1 else
                'DESTINO_NAO_ENCONTRADO_GTFS' if not opcoes else 'DESTINO_AMBIGUO_GTFS'))
        circuito = None
        if len(candidatas) == 1 and not opcoes:
            rota = candidatas[0]['route_id']
            cad = catalogo.get(chave)
            circuito = circuitos.get(rota)
            ts = por_rota[rota]
            nomes = {normalizar_destino(linha.get(k)) for k in ('lt0', 'lt1')}
            conferido = (cad is not None and cad.get('lc') is True and circuito is not None
                and f"{cad.get('lt')}-{cad.get('tl')}" == linha['c']
                and normalizar_destino(cad.get('tp')) == normalizar_destino(linha.get('lt0'))
                and normalizar_destino(cad.get('ts')) == normalizar_destino(linha.get('lt1'))
                and {t.get('direction_id') for t in ts} == {circuito['sentido_gtfs']}
                and all(normalizar_destino(t.get('trip_headsign')) in nomes - {''} for t in ts))
            if conferido:
                e.update(linha_codigo=rota, sentido_gtfs=circuito['sentido_gtfs'],
                         motivo='CIRCUITO_UNICO_GTFS_CONFIRMADO')
            else:
                circuito = None
        entradas.append(e)
        evidencias.append(dict(**e, letreiro=linha['c'], destino_olho_vivo=destino,
                               candidatos_gtfs=[dict(route_id=r, direction_id=s) for r, s in sorted(opcoes)],
                               circuito_conferido=circuito))
    return entradas, evidencias
