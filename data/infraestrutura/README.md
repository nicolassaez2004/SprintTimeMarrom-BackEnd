# Infraestrutura de ônibus de São Paulo

Snapshots de entrada (`GeoJSON` ou `GeoJSON.gz`) ficam neste diretório e não
devem ser sobrescritos; cada arquivo importado recebe SHA-256 e versão própria.

## Fontes e reprodução

- GeoSampa: [Corredores](https://metadados.geosampa.prefeitura.sp.gov.br/geonetwork/srv/api/records/8481860e-91ac-4db4-bcd2-b4ed22ea9fd5), camadas WFS `geoportal:corredor_onibus` e `geoportal:faixa_onibus`, EPSG:31983 (declarado no próprio GeoJSON; o importador rejeita `crs_origem` divergente). Captura conferida em 2026-11-26: 45 corredores (40 "Em Operação", 3 "Futuro", 2 "Em Implantação"; 12 da EMTU, alguns fora do município) e 7.378 faixas (7.003 `EXCLUSIVA`, 231 `PREFERENCIAL`, 103 `REVERSÍVEL`, 41 `TEMPORARIA`). Ex.: `.../geoportal/wfs?service=WFS&version=2.0.0&request=GetFeature&typeNames=geoportal:corredor_onibus&outputFormat=application/json`.
- OpenStreetMap: consulte as vias de São Paulo no [Overpass Turbo](https://overpass-turbo.eu/) ou via Overpass API; filtre `highway` junto a `lanes:bus`, `bus:lanes`, `psv:lanes` ou `busway` e exporte a geometria como GeoJSON. Registre a data/hora do snapshot e a URL da instância Overpass.

Para o worker importar snapshots no início de cada mês, configure
`INFRAESTRUTURA_SNAPSHOT_MANIFEST` apontando para um JSON no formato abaixo.
Mantenha arquivos novos, com nome imutável, e atualize `versao_fonte`,
`arquivo` e `capturada_em` (ISO 8601 com fuso: o instante real do download) após
obter cada captura. O worker importa uma vez por mês civil de São Paulo; cada
versão registra a anterior **da mesma fonte e camada**, SHA-256 do arquivo,
arquivo de origem, `id_field`, filtro, feições excluídas pelo filtro, geometrias
repetidas e a lista de feições adicionadas/removidas ou com geometria/atributos
alterados. Uma feição que muda de camada (faixa → corredor) aparece como
REMOVIDA numa camada e ADICIONADA na outra; no trecho, a mudança aparece na
classificação de cada data. Depois da importação OK, o worker classifica a rede
GTFS vigente com os snapshots vigentes (uma vez por par rede/conjunto, anotado
no recibo `atualizacoes_mensais_infraestrutura`). A função `consultar_em`
permite ler a versão válida numa data passada.

`filtro` (`{atributo: [valores aceitos]}`) evita importar como existente o que
não é: no exemplo, só corredores "Em Operação" e faixas `EXCLUSIVA`. **Esses
valores aguardam aprovação do PO** (ver `docs/reviews/US07-US08-review.md`).
Coordenadas fora do envelope da Grande São Paulo após a conversão (CRS errado ou
latitude/longitude trocadas), geometria ausente/não linear e identificador
repetido rejeitam o arquivo inteiro.

```json
{
  "bases": [
    {
      "arquivo": "snapshots/corredor_onibus-2026-11-26.geojson",
      "fonte": "GeoSampa",
      "url_fonte": "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/wfs",
      "versao_fonte": "geoportal:corredor_onibus 2026-11-26",
      "crs_origem": "EPSG:31983",
      "categoria": "CORREDOR",
      "capturada_em": "2026-11-26T17:52:00Z",
      "id_field": "cd_identificador",
      "filtro": {"dc_tipo_status_corredor_onibus": ["Em Operação"]}
    },
    {
      "arquivo": "snapshots/faixa_onibus-2026-11-26.geojson",
      "fonte": "GeoSampa",
      "url_fonte": "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/wfs",
      "versao_fonte": "geoportal:faixa_onibus 2026-11-26",
      "crs_origem": "EPSG:31983",
      "categoria": "FAIXA_EXCLUSIVA",
      "capturada_em": "2026-11-26T17:52:00Z",
      "id_field": "cd_identificador",
      "filtro": {"nm_tipo_faixa_corredor": ["EXCLUSIVA"]}
    }
  ]
}
```

GeoSampa distingue corredores (faixa segregada) de faixas exclusivas
compartilhadas; OSM é complementar. Não unir linhas sobrepostas nem substituir
a categoria oficial silenciosamente: manter cada fonte/versão separada para
auditar diferenças. As geometrias persistidas são linhas GeoJSON em EPSG:4326,
comparáveis às geometrias de `Trecho`.

## Classificar os trechos

Depois de importar a rede GTFS e os snapshots, execute:

```bash
python -m app.contexto.infraestrutura --versao-gtfs ID_DA_VERSAO --data AAAA-MM-DD
```

Uma associação é gravada para cada trecho, incluindo `SEM_COBERTURA`; quando há
cobertura conhecida em apenas parte do segmento, o resultado é
`COBERTURA_PARCIAL` e mantém as categorias confirmadas. `MISTO` indica corredor
e faixa exclusiva observados no mesmo trecho, com cobertura suficiente. A
classificação `VIA_COMUM` só poderá ser emitida depois que existir uma base
completa de vias comuns; a ausência de uma feição nas bases atuais não satisfaz
essa condição. O detalhamento de um trecho fica disponível em
`GET /trechos/{trecho_id}/infraestrutura?data=AAAA-MM-DD`.

Regra espacial `us07-t02-espacial-1.1` (a 1.1 só acrescenta o registro de
divergências; a identidade do conjunto inclui a versão da regra, então mudar a
regra grava ao lado sem sobrescrever): mede em EPSG:31983; considera uma
feição candidata quando a faixa de 10 m ao redor dela cobre ao menos 5 m do
trecho; mescla feições duplicadas da mesma categoria antes de calcular frações;
classifica como `CORREDOR`, `FAIXA_EXCLUSIVA` ou `MISTO` apenas quando pelo menos
99% do comprimento está coberto. Categorias conhecidas abaixo desse limiar
resultam em `COBERTURA_PARCIAL`; sem associação acima do mínimo, em
`SEM_COBERTURA`. Quando GeoSampa e OSM se sobrepõem, GeoSampa prevalece, e OSM
complementa somente fora da cobertura oficial; a porção OSM superada fica em
`feicoes_associadas` com `uso: SUPERADA_POR_GEOSAMPA` e os comprimentos
concordante/divergente. Trechos sem comprimento não bloqueiam a rede: ficam em
`nao_classificados`. O resumo traz `percentual_classificado` (CORREDOR,
FAIXA_EXCLUSIVA ou MISTO ÷ trechos). A precedência GeoSampa > OSM, a tolerância
de 10 m e o mínimo de 5 m aguardam aprovação do PO. Uma base de corredores/faixas
positivos não cobre vias comuns; estas continuam sem classificação de uso até
que uma fonte completa de vias permita confirmar essa classe.
