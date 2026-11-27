"""Caderno CONTROLADO US19-T04. SQLite em memória; aceite institucional pendente.

Não usa o banco configurado, não migra a base da equipe e não aprova parâmetros
do PO. Os serviços reais da US19 são comparados com referências manuais.
"""
import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import event

from app import create_app, db
from app.core.tempo import FUSO
from app.indicadores.propagacao import (
    INDICADOR_CORRELACAO, INDICADOR_MENSAL, INDICADOR_VARIACAO, ViagemVeiculo,
    agregar_mes, calcular_propagacao, correlacionar_chegada_partida,
    montar_correlacao, publicar_correlacao, publicar_propagacao, viagem_do_veiculo,
)
from app.models import (
    ArquivoArmazenado, ExecucaoColeta, ImportacaoProgramacao, IndicadorOperacional,
    Linha, Parada, PartidaProgramada, PassagemEstimada, RegistroBrutoGPS,
    Trecho, VersaoGTFS, ViagemObservada,
)

RAIZ = Path(__file__).resolve().parents[1]
CADERNO = RAIZ / 'docs/validacoes/US19-T04-casos.json'
REDE = '00000000-0000-0000-0000-000000000019'
LINHA = '8000-10'
AGORA = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
uid = lambda nome: str(uuid5(NAMESPACE_URL, 'us19-controle:' + nome))


def carregar_caderno():
    c = json.loads(CADERNO.read_text(encoding='utf-8'))
    assert c['origem'] == 'SINTETICA_CONTROLADA'
    assert c['aceite_institucional'] == 'PENDENTE'
    assert [r['id'] for r in c['casos']] == [f'C{i:02}' for i in range(1, 21)]
    return c


def instante(horario, dia=date(2026, 11, 24)):
    h, m, s = map(int, horario.split(':'))
    return (datetime.combine(dia, time(), FUSO) + timedelta(hours=h, minutes=m, seconds=s)).astimezone(timezone.utc)


def _exigir_sqlite():
    if db.engine.dialect.name != 'sqlite' or db.engine.url.database not in (None, '', ':memory:'):
        raise RuntimeError('Este ensaio exige SQLite em memória, sem banco externo.')


@contextmanager
def ambiente_controlado():
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite://', 'MYSQL_SSL_CA': None,
                      'PROPAGACAO_RANKING_METRICA': None, 'PROPAGACAO_INTERVALO_MAXIMO_SEG': None,
                      'INDICADORES_TRUSTED_PROXY_CIDRS': ''})
    with app.app_context():
        _exigir_sqlite()
        event.listen(db.engine, 'connect', lambda c, _: c.execute('PRAGMA foreign_keys=ON'))
        db.create_all()
        try:
            yield app
        finally:
            db.session.remove()
            db.engine.dispose()


def preparar_rede(dia=date(2026, 11, 24)):
    _exigir_sqlite()
    arquivo = ArquivoArmazenado(id=uid('arquivo'), chave='controle/rede-us19.zip',
        nome_original='rede-sintetica.zip', mime_type='application/zip', tamanho_bytes=0, sha256='a'*64, criado_em=AGORA)
    versao = VersaoGTFS(id=REDE, hash='a'*64, arquivo=arquivo, vigencia_inicio=date(2026, 1, 1))
    linha = Linha(versao_gtfs=versao, codigo=LINHA, nome='Linha SINTÉTICA US19')
    db.session.add_all([versao, linha]); db.session.flush()
    importacao = ImportacaoProgramacao(id=uid('importacao'), versao_gtfs_id=REDE, linha=linha,
        data_operacao=dia, sentido='0', tipo_dia='UTIL', fonte='GTFS', versao_fonte='controle-us19',
        fonte_url='urn:ensaio-sintetico:us19', arquivo=arquivo, capturada_em=AGORA, verificada_em=AGORA)
    paradas = [Parada(versao_gtfs_id=REDE, codigo=c, nome=f'Parada de controle {c}',
                      latitude=-23.55, longitude=-46.7+i*.001) for i, c in enumerate('ABCDEF')]
    db.session.add_all([importacao, *paradas]); db.session.flush()
    trechos = [Trecho(id=f'T-{a}{b}', versao_gtfs_id=REDE, linha_codigo=LINHA, sentido='0',
        trajeto='CONTROLE', ordem=i+1, parada_origem_codigo=a, parada_destino_codigo=b,
        geometria=[[-46.7+i*.001, -23.55], [-46.7+(i+1)*.001, -23.55]])
        for i, (a, b) in enumerate(zip('ABCDE', 'BCDEF'))]
    db.session.add_all(trechos); db.session.commit()
    return importacao, trechos


def criar_viagem(importacao, nome, previstos, observadas, *, prefixo='5810', oferta='FIXA'):
    """Passagens sintéticas já estimadas; GPS anterior/posterior reais no SQLite.

    Não revalida a interpolação US15. Aqui ela é entrada, como no serviço US19.
    """
    partida = PartidaProgramada(id=uid('partida:'+nome), importacao=importacao,
        ordem=len(importacao.partidas)+1, saida_prevista=instante(previstos['A'], importacao.data_operacao),
        tipo_oferta=oferta, intervalo_seg=600 if oferta == 'FREQUENCIA' else None)
    viagem = ViagemObservada(id=uid('viagem:'+nome), partida_programada=partida,
        saida_observada=instante(observadas.get('A', previstos['A']), importacao.data_operacao),
        cobertura_tracado=1, versao_reconstrucao='recon-controle', versao_correspondencia='match-controle')
    ciclo = ExecucaoColeta(iniciada_em=viagem.saida_observada, prevista_em=viagem.saida_observada, estado='OK', recebidos=2*len(observadas))
    db.session.add_all([partida, viagem, ciclo]); db.session.flush()
    passagens = []
    for codigo, horario in observadas.items():
        quando = instante(horario, importacao.data_operacao)
        gps = [RegistroBrutoGPS(execucao=ciclo, prefixo_origem=prefixo, linha_origem=LINHA,
            horario_gps=quando+timedelta(seconds=delta), coletado_em=quando+timedelta(seconds=delta),
            latitude=-23.55, longitude=-46.7, payload={'origem':'SINTETICA'}) for delta in (-1, 1)]
        db.session.add_all(gps); db.session.flush()
        p = PassagemEstimada(id=uid('passagem:'+nome+codigo), viagem_observada_id=viagem.id,
            versao_gtfs_id=REDE, parada_codigo=codigo, instante=quando,
            metodo='INTERPOLACAO_LINEAR_PERCURSO', versao_estimativa='estimativa-controle',
            posicao_anterior_id=gps[0].id, posicao_posterior_id=gps[1].id)
        db.session.add(p); passagens.append(p)
    db.session.commit()
    return viagem, passagens


def sem_rastro(p):
    return PassagemEstimada(**{c: getattr(p, c) for c in ('id', 'viagem_observada_id', 'versao_gtfs_id',
        'parada_codigo', 'instante', 'metodo', 'versao_estimativa', 'posicao_anterior_id')}, posicao_posterior_id=None)


def conferir_variacao(caso, app):
    dia = date.fromisoformat(caso.get('data_operacao', '2026-11-24'))
    imp, trechos = preparar_rede(dia)
    previstos = caso.get('previstos', {'A':'09:00:00', 'B':'09:04:00'})
    viagem, passagens = criar_viagem(imp, caso['id'], previstos, caso['observadas'], oferta=caso.get('oferta', 'FIXA'))
    if caso.get('sem_rastreabilidade'):
        passagens = [sem_rastro(p) if p.parada_codigo == caso['sem_rastreabilidade'] else p for p in passagens]
    usados = {k:v for k,v in previstos.items() if k != caso.get('sem_previsto')}
    resultado = calcular_propagacao(viagem, trechos[:1], passagens, usados)
    t = resultado.trechos[0]
    obtido = {'origem': t.atraso_origem_seg, 'destino': t.atraso_destino_seg, 'variacao': t.variacao_seg, 'motivo': t.motivo}
    assert obtido == {k:caso['esperado'][k] for k in obtido}, (caso['id'], obtido)
    if 'mes' in caso['esperado']:
        publicar_propagacao(resultado)
        agregar_mes('2026-11', 'MEDIA'); agregar_mes('2026-12', 'MEDIA')
        app.config['PROPAGACAO_RANKING_METRICA'] = 'MEDIA'
        cliente = app.test_client()
        assert cliente.get('/atrasos/ranking?mes=2026-11').json['dados'][0]['aumento_atraso'] == 180
        assert cliente.get('/atrasos/ranking?mes=2026-12').json['dados'] == []
        obtido['mes'] = resultado.data_operacao.strftime('%Y-%m')
    return obtido


def vv(nome, saida, chegada, *, ac=0, ap=0, prefixos=('5810',)):
    base = instante('09:00:00')
    s = base+timedelta(minutes=saida); c = base+timedelta(minutes=chegada)
    return ViagemVeiculo(nome, 'partida-'+nome, frozenset(prefixos), LINHA, '0', REDE,
        date(2026,11,24), s, ap, 'OFERTA_POR_FREQUENCIA' if ap is None else None,
        c, ac, None, c, 'recon-controle', 'match-controle', 'estimativa-controle')


def conferir_encadeamento(caso):
    tipo = caso['cenario']
    if tipo == 'integracao_veiculo':
        imp, trechos = preparar_rede()
        objetos = [criar_viagem(imp, 'anterior', {'A':'09:00:00','B':'09:38:00'}, {'A':'09:00:00','B':'09:40:00'}),
                   criar_viagem(imp, 'seguinte', {'A':'09:52:00','B':'10:28:00'}, {'A':'09:55:00','B':'10:30:00'})]
        entradas = [viagem_do_veiculo(v, calcular_propagacao(v, trechos[:1], ps, previstos))
                    for (v, ps), previstos in zip(objetos, ({'A':'09:00:00','B':'09:38:00'}, {'A':'09:52:00','B':'10:28:00'}))]
        aliases = {v.viagem_id:nome for v,nome in zip(entradas, ('anterior','seguinte'))}
    elif tipo == 'prefixos_distintos': entradas = [vv('a',0,40), vv('b',55,90,prefixos=('9999',))]
    elif tipo == 'troca_veiculo': entradas = [vv('a',0,10), vv('b',20,30,prefixos=('5810','7777')), vv('c',40,50)]
    elif tipo == 'sobrepostas': entradas = [vv('b',30,60), vv('a',0,40)]
    else: entradas = [vv('a',0,10), vv('b',20,30,ap=None), vv('c',40,50)]
    r = correlacionar_chegada_partida(entradas, timedelta(minutes=30))
    nome = lambda v: aliases[v.viagem_id] if tipo == 'integracao_veiculo' else v.viagem_id
    obtido = {'pares':[[nome(p.anterior),nome(p.seguinte)] for p in r.pares]}
    if tipo == 'integracao_veiculo':
        p = r.pares[0]
        assert p.prefixo == '5810'
        obtido.update(intervalo_seg=p.intervalo_seg, atraso_chegada=p.anterior.atraso_chegada_seg, atraso_partida=p.seguinte.atraso_partida_seg)
    else: obtido['descartados'] = [[nome(p.anterior),nome(p.seguinte),p.motivo] for p in r.descartados]
    assert obtido == caso['esperado'], (caso['id'], obtido)
    return obtido


def conferir_escala(caso, caderno):
    preparar_rede()
    inicio, fim = map(datetime.fromisoformat, (caso['inicio'], caso['fim']))
    viagens = []
    for par in caderno['pares_das_escalas']:
        chegada = datetime.fromisoformat(par['chegada'])
        if not inicio <= chegada < fim: continue
        a = vv(par['id']+'-a', 0, 20, ac=par['x'], prefixos=(par['id'],))
        b = vv(par['id']+'-b', 25, 35, ap=par['y'], prefixos=(par['id'],))
        viagens.extend([replace(a, saida=chegada-timedelta(minutes=20), chegada=chegada, termino=chegada),
                        replace(b, saida=chegada+timedelta(minutes=5), chegada=chegada+timedelta(minutes=15), termino=chegada+timedelta(minutes=15))])
    r = correlacionar_chegada_partida(list(reversed(viagens)), timedelta(minutes=30))
    pares = [[p.anterior.viagem_id,p.seguinte.viagem_id] for p in r.pares]
    esperado = caso['esperado']
    assert pares == esperado['pares'] and r.amostra == esperado['n']
    assert r.motivo_sem_valor == esperado['motivo']
    assert r.coeficiente is None if esperado['r'] is None else abs(r.coeficiente-esperado['r']) < 1e-12
    contexto = dict(linha=LINHA, inicio=inicio, fim=fim, versao_gtfs_id=REDE)
    publicado = montar_correlacao(r, **contexto)
    assert publicado['contagem_numerador'] == publicado['contagem_denominador'] == esperado['n']
    publicar_correlacao(r, **contexto); publicar_correlacao(r, **contexto)
    assert IndicadorOperacional.query.filter_by(indicador=INDICADOR_CORRELACAO).count() == 1
    persistido = IndicadorOperacional.query.filter_by(indicador=INDICADOR_CORRELACAO).one()
    assert persistido.valor == r.coeficiente and persistido.unidade == 'r (adimensional)'
    assert persistido.periodo_inicio == inicio.astimezone(timezone.utc) and persistido.periodo_fim == fim.astimezone(timezone.utc)
    return {'pares':pares,'n':r.amostra,'r':r.coeficiente,'motivo':r.motivo_sem_valor,
            'unidade':publicado['unidade'], 'inicio':publicado['periodo_inicio'].isoformat(), 'fim':publicado['periodo_fim'].isoformat()}


def semear_ranking(app):
    """Fluxo real T01 → persistência → T03; também serve à evidência do frontend."""
    imp, trechos = preparar_rede()
    entradas = [({'A':'09:00:00','B':'09:04:00','C':'09:08:00','D':'09:12:00','E':'09:16:00','F':'09:20:00'},
                 {'A':'09:02:00','B':'09:09:00','C':'09:13:00','D':'09:16:00','E':'09:20:00'}),
                ({'A':'09:15:00','B':'09:19:00','C':'09:23:00','D':'09:27:00','E':'09:31:00','F':'09:35:00'},
                 {'A':'09:15:00','B':'09:19:00','C':'09:33:00','D':'09:37:00','E':'09:41:00'}),
                ({'A':'09:30:00','B':'09:34:00','C':'09:38:00','D':'09:42:00','E':'09:46:00','F':'09:50:00'},
                 {'A':'09:31:00','B':'09:36:00','C':'09:42:00'})]
    for i, (previstos, observadas) in enumerate(entradas):
        viagem, passagens = criar_viagem(imp, f'ranking-{i}', previstos, observadas, prefixo=f'CONTROLE-{i}')
        p = calcular_propagacao(viagem, trechos, passagens, previstos)
        publicar_propagacao(p); publicar_propagacao(p)
    assert IndicadorOperacional.query.filter_by(indicador=INDICADOR_VARIACAO).count() == 15
    for metrica in ('MEDIA','MEDIANA'):
        agregar_mes('2026-11', metrica); agregar_mes('2026-11', metrica)
    for r in IndicadorOperacional.query.filter_by(indicador=INDICADOR_MENSAL):
        r.qualidade = {**r.qualidade, 'origem':'SINTETICA'}
    db.session.commit()
    app.config['PROPAGACAO_RANKING_METRICA'] = 'MEDIA'  # Somente este app de ensaio.


def conferir_ranking(caso, app):
    semear_ranking(app)
    cliente = app.test_client(); capturas = {}
    for metrica in ('MEDIA','MEDIANA'):
        app.config['PROPAGACAO_RANKING_METRICA'] = metrica
        r = cliente.get('/atrasos/ranking?mes=2026-11&linha=8000-10&sentido=0')
        assert r.status_code == 200
        corpo = r.json
        ordem = [[r['trecho']['id'],r['aumento_atraso']] for r in corpo['dados']]
        assert ordem == caso['esperado'][metrica.lower()]
        assert corpo['trechos_sem_valor'] == caso['esperado']['sem_valor']
        assert corpo['dados'][3]['cobertura'] == caso['esperado']['cobertura_cd']
        assert [r['cobertura'] for r in corpo['dados']] == [1,1,2/3,2/3]
        assert [r['posicao'] for r in corpo['dados']] == [1,2,3,4]
        persistidos = {r.trecho_id:r for r in IndicadorOperacional.query.filter_by(
            indicador=INDICADOR_MENSAL, versao_calculo='aumento-atraso-mensal-1.0:'+metrica)}
        for item in corpo['dados']:
            gravado = persistidos[item['trecho']['id']]
            assert (item['aumento_atraso'], item['unidade'], item['amostras'], item['amostras_total'],
                    item['versao_conjunto'], item['versao_gtfs_id'], item['versao_correspondencia'], item['versao_calculo']) == (
                    gravado.valor, gravado.unidade, gravado.contagem_numerador, gravado.contagem_denominador,
                    gravado.versao_conjunto, gravado.versao_gtfs_id, gravado.versao_correspondencia, gravado.versao_calculo)
        capturas[metrica] = corpo
        alias = cliente.get('/api/atrasos/ranking?mes=2026-11&linha=8000-10&sentido=0',
                            headers={'Origin':'http://127.0.0.1:5173'})
        assert alias.json == corpo and alias.headers['Access-Control-Allow-Origin'] == 'http://127.0.0.1:5173'
    assert IndicadorOperacional.query.filter_by(indicador=INDICADOR_MENSAL).count() == 10
    for query in ('mes=2026-10','mes=2026-11&sentido=1','mes=2026-11&linha=outra'):
        assert cliente.get('/atrasos/ranking?'+query).json['dados'] == []
    app.config['PROPAGACAO_RANKING_METRICA'] = None
    assert cliente.get('/atrasos/ranking?mes=2026-11').status_code == 409
    return capturas


def executar_caso(caso, caderno):
    with ambiente_controlado() as app:
        if caso['grupo'] == 'variacao': obtido = conferir_variacao(caso, app)
        elif caso['grupo'] == 'encadeamento': obtido = conferir_encadeamento(caso)
        elif caso['grupo'] == 'escala': obtido = conferir_escala(caso, caderno)
        else: obtido = conferir_ranking(caso, app)
    return {'id':caso['id'], 'descricao':caso['descricao'], 'referencia_manual':caso['calculo_manual'],
            'resultado':'CONFERE_NO_ENSAIO', 'esperado':caso['esperado'], 'obtido':obtido}


def executar():
    c = carregar_caderno()
    resultados = [executar_caso(caso, c) for caso in c['casos']]
    return {'data':c['data'], 'origem':c['origem'], 'python':platform.python_version(), 'banco':'SQLite em memória, chaves estrangeiras ativas',
        'sha256_caderno_utf8_lf':sha256(CADERNO.read_text(encoding='utf-8').encode('utf-8')).hexdigest(),
        'resultados':resultados, 'total':len(resultados), 'divergencias_controles':0,
        'parametros_ensaio':c['parametros_apenas_do_ensaio'], 'pendencias':c['pendencias'],
        'interpretacao':'ASSOCIACAO_SEM_INFERENCIA_CAUSAL', 'aceite_institucional':'PENDENTE',
        'revisao_por_outra_pessoa':'PENDENTE', 'pode_mover_para_pronto':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saida', type=Path)
    args = parser.parse_args()
    resultado = executar()
    if args.saida:
        args.saida.parent.mkdir(parents=True, exist_ok=True)
        args.saida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'total':resultado['total'], 'divergencias_controles':0, 'aceite_institucional':'PENDENTE', 'pode_mover_para_pronto':False}))
