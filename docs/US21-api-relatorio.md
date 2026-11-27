# US21 — Consulta do relatório mensal de associações

Criada em 17/11/2026; iniciada em 27/11/2026; responsável mantido; conclusão pendente de revisão por outra pessoa.

`GET /relatorios/associacoes?mes=AAAA-MM` (também disponível em `/api/relatorios/associacoes`) é uma consulta pública somente de leitura. O parâmetro `mes` é obrigatório e não pode ser repetido. O mês é a **referência do relatório**, não uma indicação de que o modelo foi ajustado apenas com observações daquele mês: `fontes` informa todos os meses e snapshots do painel usado.

A rota consulta um snapshot já registrado; não roda regressão, não grava nem aprova um resultado. Na implantação, aplicar a migração `us21_0027` com `flask --app app db upgrade`. A rota usa o limite compartilhado por IP e aceita CORS do frontend local, como as demais consultas públicas.

## Resposta para a interface

- **200 — `PUBLICADO`**: `mes`, `situacao`, `registrado_em` (UTC), `fontes`, `metodo`, `resultado` e `revisao`. Cada elemento de `fontes` contém mês, hash e versão da base, `selecao` (fontes, rede e versões) e política. Preserve essa lista: ela descreve todo o painel de entrada.
- `metodo` identifica o estimador OLS associacional, pesos, desfecho, métrica, unidade e interpretação.
- `resultado.modelos.principal` e `.secundario`: cada um contém `observacoes` (tamanho da amostra em trechos/mês), exclusões, quantidade e tamanhos de grupos, categorias de referência, graus de liberdade, valor crítico e `limitacoes`. Em `coeficientes`, cada variável tem `coeficiente`, `erro_padrao`, `ic95` como `[limite_inferior, limite_superior]` e `p_valor`. Chuva está em incremento de 10 mm; velocidade em km/h; o desfecho está em segundos. O modelo secundário usa velocidade dos próprios ônibus como proxy.
- `resultado.parametros` registra método de incerteza, nível 0,95, correção, referência crítica, duração e origem dos blocos, mínimos e referência/hash do mapa espacial. `resultado.hash_plano`, `hashes_bases`, `versao_calculo` e `versoes` identificam entradas e bibliotecas. Para reproduzir, é preciso recuperar os snapshots e o mapa identificados por esses hashes e referência; a API não os inclui integralmente.
- `resultado.pode_publicar_resultados` permanece `false`: o cálculo sozinho não libera publicação. O estado público `PUBLICADO` decorre da `revisao` separada, vinculada pelo `hash_resultado`, com diagnósticos e publicação aprovados. A conferência da identidade e legitimidade da revisão é humana; hash comprova integridade, não identidade.
- **404 — `NAO_ENCONTRADO`**: não existe registro para o mês. Inclui `mes` e `mensagem`, sem `resultado`.
- **409 — `INVALIDO`**: análise inválida ou registro inconsistente; inclui `mes` e `motivo`, sem coeficientes. Não tratar como zero nem como relatório válido.
- **400**: parâmetros inválidos; corpo `erro` com `codigo`, `mensagem` e `campo`. **429**: limite por IP; observar `Retry-After`. **503**: consulta ou limitador indisponível. Não converter falhas em ausência de relatório.

Exemplos reais de resposta HTTP obtidos em teste isolado estão em [US21-relatorios-api-ensaio.json](evidencias/US21-relatorios-api-ensaio.json), com uma resposta 200 e outra 404. Dados, nomes e aceites nesse arquivo são **fictícios**, apenas para ensaio.

## Registro interno

Depois de um cálculo US21-T03B válido e da revisão humana de dependência residual, influência e publicação, um processo interno chama `registrar_publicado(mes, resultado, bases, revisao)` em `app.relatorios.associacoes` dentro de uma transação e faz `db.session.commit()`. `revisao` deve conter `responsavel`, `data` ISO, `referencia`, `hash_resultado` calculado com `app.analises.associacoes.digest(resultado)`, e `diagnosticos_aprovados` e `publicacao_aprovada` iguais a `true`. Não há rota pública de escrita. O serviço verifica resultado, revisão, hashes das bases, mês no painel e integridade do snapshot; bases declaradas sintéticas são recusadas. Um processo interno pode registrar uma análise inválida via `registrar_invalido(mes, motivo)` e substituí-la posteriormente por uma publicação revisada. Um relatório já publicado não é sobrescrito; versionamento/substituição requer fluxo futuro.

O plano versionado atual ainda não contém os parâmetros e registros necessários para gerar um resultado real liberado. Sem esses dados e a revisão posterior, não existe relatório real a publicar. A consulta não valida identidade de quem forneceu a revisão nem automatiza diagnósticos estatísticos. Associação observacional não prova causalidade.
