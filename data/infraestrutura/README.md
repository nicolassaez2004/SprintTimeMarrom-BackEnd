# Infraestrutura de ônibus de São Paulo

Snapshots de entrada (`GeoJSON` ou `GeoJSON.gz`) ficam neste diretório e não
devem ser sobrescritos; cada arquivo importado recebe SHA-256 e versão própria.

## Fontes e reprodução

- GeoSampa: [Corredores](https://metadados.geosampa.prefeitura.sp.gov.br/geonetwork/srv/api/records/8481860e-91ac-4db4-bcd2-b4ed22ea9fd5), camada `corredor_onibus`, EPSG:31983. O catálogo registra revisão em 2025-10-10. Baixe também a camada “Faixas exclusivas” no catálogo/GeoSampa, preservando a data da captura e o arquivo original.
- OpenStreetMap: consulte as vias de São Paulo no [Overpass Turbo](https://overpass-turbo.eu/) ou via Overpass API; filtre `highway` junto a `lanes:bus`, `bus:lanes`, `psv:lanes` ou `busway` e exporte a geometria como GeoJSON. Registre a data/hora do snapshot e a URL da instância Overpass.

Para o worker importar snapshots no início de cada mês, configure
`INFRAESTRUTURA_SNAPSHOT_MANIFEST` apontando para um JSON no formato abaixo.
Mantenha arquivos novos, com nome imutável, e atualize `versao_fonte` e
`arquivo` após obter cada captura. O worker importa uma vez por mês; cada versão
registra a anterior, SHA-256 e a lista de feições adicionadas/removidas ou com
geometria/atributos/categoria alterados. A função `consultar_em` permite ler a
versão válida numa data passada.

```json
{
  "bases": [
    {
      "arquivo": "snapshots/corredores.geojson",
      "fonte": "GeoSampa",
      "url_fonte": "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/wfs",
      "versao_fonte": "2025-10-10",
      "crs_origem": "EPSG:31983",
      "categoria": "CORREDOR",
      "id_field": "id"
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

Regra espacial `us07-t02-espacial-1.0`: mede em EPSG:31983; considera uma
feição candidata quando a faixa de 10 m ao redor dela cobre ao menos 5 m do
trecho; mescla feições duplicadas da mesma categoria antes de calcular frações;
classifica como `CORREDOR`, `FAIXA_EXCLUSIVA` ou `MISTO` apenas quando pelo menos
99% do comprimento está coberto. Categorias conhecidas abaixo desse limiar
resultam em `COBERTURA_PARCIAL`; sem associação acima do mínimo, em
`SEM_COBERTURA`. Quando GeoSampa e OSM se sobrepõem, GeoSampa prevalece, e OSM
complementa somente fora da cobertura oficial. Uma base de corredores/faixas
positivos não cobre vias comuns; estas continuam sem classificação de uso até
que uma fonte completa de vias permita confirmar essa classe.
