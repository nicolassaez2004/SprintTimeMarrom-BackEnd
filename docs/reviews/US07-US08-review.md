# Review integrada — US07 e US08 (Sprint 3)

Branch `review/us07-us08-contexto` (a partir de `main` em `4c7b9cb`, sem commit).
Data: 26/11/2026. Todas as seis tarefas já estavam integradas à `main`.

## Parecer por tarefa

| Tarefa | Problemas encontrados | Correções | Testes | Parecer |
|---|---|---|---|---|
| US07-T01 Importação | P0: aceitava coordenadas fora de SP (UTM declarado como graus, lat/lon trocadas). P0 (dado real): importava corredores "Futuro"/"Em Implantação" e faixas PREFERENCIAL/REVERSÍVEL/TEMPORÁRIA como infraestrutura existente. P1: relatório de mudanças comparava a camada de faixas com a de corredores. P2: ID repetido → `IntegrityError` bruto; geometria ausente → `AttributeError`; CRS do arquivo não conferido; arquivo de origem, `id_field` e duplicatas geométricas não registrados. | Envelope de plausibilidade da Grande SP; CRS do GeoJSON conferido; erros claros por feição; `filtro` de atributos registrado e parte da identidade da importação; comparação só com a mesma fonte e camada; metadados de reprodução. | 13 novos + captura real do GeoSampa | **PARCIAL**: valores do filtro e captura OSM pendentes |
| US07-T02 Classificação | P1 (dado real): **33 trechos de comprimento zero na rede real abortavam a classificação da rede inteira**. P1: mudar a regra não reclassificava (o conjunto ignorava a versão da regra). P2: divergência OSM×GeoSampa resolvida sem registro. P1 (PO): cruzamentos promovem trechos a MISTO/PARCIAL. | Trecho degenerado listado em `nao_classificados`; conjunto inclui `VERSAO_REGRA` (1.1); OSM superada fica em `feicoes_associadas` com comprimentos concordante/divergente; `percentual_classificado` no resumo. | 6 novos + rede real (96.808 trechos) | **PARCIAL**: critério de 100% não atingível sem base de vias comuns |
| US07-T03 Atualização mensal | P1: atualização não reclassificava os trechos (API devolvia NAO_PROCESSADO no mês novo). P2: mês calculado em UTC; `capturada_em` do manifesto não era aceito. | Worker classifica a rede GTFS vigente uma vez por (rede, conjunto) e anota no recibo; mês civil de São Paulo; `capturada_em` ISO e `filtro` no manifesto. | Teste obrigatório A→B + 3 | **PARCIAL**: não comprovada em produção (migrações não aplicadas na Aiven) |
| US08-T01 Velocidades | Nenhum defeito de cálculo. Limites de plausibilidade são `None` por padrão (corretamente sem números inventados). | — | 22 existentes + conferência geodésica | **APROVÁVEL** (método) / limites pendentes do PO |
| US08-T02 Janelas 15 min | P1: os limites da T01 não entravam no escopo/chave; quando o PO definir ou alterar a plausibilidade, os resultados antigos seriam sobrescritos. P2: chave dependia do fuso do período pedido. P1: `downgrade` de `us08_0024` falha em MySQL (erro 1553). | `versoes.calculo` no escopo (grava ao lado); chave em UTC; downgrade remove só a tabela. | 3 novos + 1 | **PARCIAL**: método da média pendente; sem orquestração |
| US08-T03 API | Nenhum defeito. Somente leitura, parametrizada, null com motivo, fonte identificada como proxy. Não há Waze. | Só reflexo de `calculo` nas séries. | 10 existentes + evidência | **APROVÁVEL** (contrato) / depende da T02 popular dados |

## Evidências

**Testes.** Específicos: 120 passam (`tests/contexto`, `test_velocidade_*_us08_*`, `test_trafego_api_us08_t03`).
Suíte completa: **1071 passed, 10 skipped, 0 falhas** (linha de base 1046 passed).
Todos sintéticos em SQLite, salvo o indicado abaixo.

**Migrações (MariaDB descartável, não a Aiven).** Cadeia completa até `us08_0024` e
`downgrade us15_0019` → `upgrade` head. Antes da correção o downgrade falhava com o erro 1553.
FK impede associação/medição órfã (1452), CHECK rejeita `VIA_COMUM` (4025), RESTRICT
impede apagar versão referenciada (1451). Aiven (só leitura): `us29_0012`. US07/US08
(e US15) **não estão aplicadas** lá.

**Importação real GeoSampa (WFS, 26/11/2026 17:52Z, SQLite local).**

| Camada | Feições | Com filtro | Excluídas | Duplicatas geométricas |
|---|---|---|---|---|
| `corredor_onibus` (EPSG:31983) | 45 | 40 (`Em Operação`) | 3 Futuro, 2 Em Implantação | 0 |
| `faixa_onibus` (EPSG:31983) | 7.378 | 7.003 (`EXCLUSIVA`) | 231 PREFERENCIAL, 103 REVERSÍVEL, 41 TEMPORARIA | 4 |

Conversão conferida: Corredor Paes de Barros em (−46,5862; −23,5842), na Mooca. O teste da
Av. Paulista faz ida e volta pelo UTM com erro < 1e-7°. OpenStreetMap **não foi capturado**
(sem snapshot real).

**Classificação da rede real** (96.808 trechos GTFS lidos da Aiven só com SELECT, classificados
localmente, 123 s):

| Classe | Com filtro | Sem filtro |
|---|---|---|
| CORREDOR | 3.126 | 3.067 |
| FAIXA_EXCLUSIVA | 20.740 | 20.953 |
| MISTO | 1.305 | 1.591 |
| COBERTURA_PARCIAL | 19.924 | 19.922 |
| SEM_COBERTURA | 51.680 | 51.242 |
| não classificados (comprimento zero) | 33 | 33 |
| **% classificado com base nas fontes** | **26,0%** | 26,46% |

O filtro muda 792 trechos (ex.: 187 FAIXA→SEM_COBERTURA, 81 MISTO→CORREDOR).
Amostra manual coerente: 637G em Corredor Parelheiros/Rio Bonito, 407N na Av. Aricanduva, 407A
na Av. Álvaro Ramos. Artefato de cruzamento: 594 de 1.305 MISTO têm a categoria minoritária abaixo de 10%
(ex.: R. Domingos de Morais com 4% de corredor; Rebouças×Paulista com 2% de faixa) e 2.420
PARCIAL têm cobertura abaixo de 10%.

**Histórico (T03, teste obrigatório).** A (out) corredor → B (nov) corredor movido + faixa nova.
Consulta em 15/10 retorna CORREDOR com fontes "A", e em 15/11 retorna FAIXA_EXCLUSIVA com "B". O relatório de
B mostra `GEOMETRIA_OU_ATRIBUTOS_ALTERADOS: 1`, e repetir os dois meses não cria associação
nem anotação nova (2 associações, 1 classificação no recibo).

**Velocidades (sintético).** A→B: 0,003° de longitude a 23,55° S. Geodésica WGS84: 306,1 m;
UTM 23S: 306,30 m (diferença < 0,1%). 30 s → **36,7558 km/h**; 60 s → 18,3779 km/h. O mesmo
valor aparece na amostra da T01, na janela da T02, no `MedicaoContexto` e na API. Janela com 20, 30 e
40 km/h (600 m cada): aritmética 30,0, espacial 27,69. Janela vazia: `null` +
`sem_registro`.

Resposta `GET /contexto/trafego` (trecho A→B, 12:00–12:45Z):
`fonte.tipo = PROXY_TRAFEGO_VELOCIDADE_ONIBUS`, `series[0].versoes = {gtfs, estimativa,
reconstrucao, correspondencia, calculo: us08-t01-v1-lacuna120-vmin1-vmax80, agregado}`,
janelas `36.7558 / 18.3779 / null (sem_registro)`, unidade `km/h`.

## Decisões pendentes do PO

1. **Filtro das bases GeoSampa** (P0 enquanto não decidido): aceitar só corredores "Em Operação"?
   "Em Implantação" conta? Faixas `PREFERENCIAL`/`REVERSÍVEL`/`TEMPORARIA` contam como
   exclusivas? O horário de funcionamento (muitas só no pico) importa?
2. **100% dos trechos** (P1): sem uma base de vias comuns, 74% dos trechos ficam sem
   classe fundamentada. Proposta: usar as vias OSM (`highway`) sem tags de ônibus como
   evidência de via comum, como nova regra versionada.
3. **Cruzamentos** (P1): um cruzamento perpendicular rende cerca de 20 m dentro da faixa de 10 m e passa
   do mínimo de 5 m. Proposta: mínimo ≥ 2 × tolerância, ou exigir alinhamento entre o trecho e a feição.
4. **Precedência GeoSampa > OSM, tolerância 10 m, mínimo 5 m e limiar 99%**: hoje escolhidos
   pelo desenvolvimento, sem aprovação registrada.
5. **US08**: limites de plausibilidade e lacuna (sem eles tudo fica `PENDENTE`), método da média
   (`ARITMETICA` × `ESPACIAL`), instante de atribuição, cobertura mínima e efeito da coleta
   incompleta (>10%).
6. **Waze**: sem acordo, nada implementado. Com acordo, serão necessárias subtarefas próprias (ingestão a cada 2
   min, fonte separada `WAZE_*` em `MedicaoContexto`).

## Pendências de integração

- P1: nenhum job executa a US15-T01 (passagens) nem a US08-T02 (só scripts e testes). Em produção,
  `/contexto/trafego` responderá `sem_resultados_persistidos` até a orquestração (US20).
- P1: aplicar as migrações `us06/us07/us08` na Aiven (responsável pelo banco).
- P2: o arquivo original da infraestrutura fica em `data/infraestrutura/` identificado por SHA-256;
  não passa pelo `ArquivoArmazenado`/FileStore.
- P2: em linhas circulares, a parada final repete a inicial e a US15 guarda uma passagem
  por código, então o último trecho sai como `ordem_temporal_invertida` (limitação da US15).
- US21: contexto pronto para reutilização sem recálculo. A infraestrutura sai por
  `AssociacaoInfraestruturaTrecho` (data → conjunto) e a velocidade por `MedicaoContexto`
  (TRAFEGO). Ambos usam `trecho_id`, que fixa a versão GTFS.

## Cartões

- Podem ir para Pronto: **US08-T01** e **US08-T03** (método e contrato), com a ressalva de
  que os valores dependem das decisões 5 do PO.
- Ficam em revisão do PO: **US07-T01/T02/T03** e **US08-T02**.
