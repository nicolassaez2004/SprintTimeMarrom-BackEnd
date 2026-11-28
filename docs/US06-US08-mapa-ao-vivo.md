# Clima e trânsito para o mapa ao vivo (US06 + US08 no mapa da US22)

As rotas `/contexto/clima` e `/contexto/trafego` respondem **um trecho por vez**. Para o mapa, que
precisa de todos os pontos de uma vez, há duas rotas que devolvem GeoJSON num instante. São
públicas, sem login, têm o limite por IP da US11 e liberam CORS para o site.

O caminho segue o diagrama de componentes: rota Flask ([app/api/mapas.py](../app/api/mapas.py)),
que só valida os parâmetros, e depois o QueryService
([app/consultas/clima.py](../app/consultas/clima.py) e
[app/consultas/trafego.py](../app/consultas/trafego.py)). As rotas só leem e nunca calculam nem
gravam medições.

## `GET /mapas/clima?instante=<ISO com fuso>[&fonte=INMET|CGE]`

- Devolve um ponto por `EstacaoMeteorologica` da fonte. O padrão é o INMET.
- Cada ponto traz a `ObservacaoClima` da última janela nativa **inteira** terminada até o
  instante: 1 hora no INMET, 15 minutos no CGE.
- `chuva_mm` é o total da janela e `temperatura_c` é a leitura no fim da janela.
- Estação sem observação: os valores vêm `null` com `motivo: "sem_observacao"`. Nunca chuva zero.
- Sem estações coletadas: `features: []` com `motivo: "sem_estacoes"`.

## `GET /mapas/trafego?instante=<ISO com fuso>&linha=<letreiro>[&sentido=0|1]`

- Devolve uma `LineString` por `Trecho` da linha, na versão GTFS vigente no dia do instante
  (`versao_vigente`, horário de São Paulo).
- Cada trecho traz a velocidade da janela de 15 minutos que contém o instante, lida de
  `MedicaoContexto` (tipo TRAFEGO, gravada pela US08-T02). Se houver mais de um cálculo para a
  janela, vale o mais recente.
- Trecho sem registro: `velocidade_media_kmh: null` com motivo. Nunca zero.
- Resposta vazia, com o motivo:
  - `sem_versao_gtfs_vigente`;
  - `versao_gtfs_ambigua`;
  - `linha_sem_trechos`.

## Testes

[tests/indicadores/test_mapa_contexto_us06_us08.py](../tests/indicadores/test_mapa_contexto_us06_us08.py),
com dados sintéticos em SQLite na memória:
- janela do clima e estação sem observação;
- separação das fontes INMET e CGE;
- velocidade por trecho e trecho sem registro;
- falta de versão GTFS e linha sem trechos;
- validação dos parâmetros;
- cabeçalho de CORS para o site.

## Situação dos dados reais

As rotas funcionam, mas ainda não há dado real para elas:
- **Clima:** o INMET exige um token, pedido por e-mail, e `INMET_TOKEN` está vazio. O CGE não tem
  API pública.
- **Trânsito:** depende do GTFS importado e de a cadeia da US08 (correspondência, reconstrução,
  passagens e velocidade) processar o GPS coletado.

Até lá, o mapa mostra o motivo, por exemplo "Ainda não há estações de clima".
