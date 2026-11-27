# US21 — Cálculo dos coeficientes

O serviço `app.analises.coeficientes.calcular_associacoes(plano, bases)` recebe o plano e os snapshots mensais da US21-T01. Antes do cálculo, verifica os aceites registrados, hashes, política, versões, cobertura e limites de grupos da US21-T02. O registro atual ainda não contém esses dados, portanto a chamada retorna `NAO_LIBERADO` e nenhum coeficiente. A confirmação verbal das aprovações não fornece os valores nem os detalhes dos registros necessários.

Os dois modelos usam mínimos quadrados com pesos iguais: chuva por 10 mm, categorias de linha/sentido, infraestrutura e mês calendário; o secundário inclui a velocidade espacial dos ônibus. As categorias de referência são escolhidas em ordem crescente e registradas no resultado. A incerteza usa covariância em duas vias com correção de amostra finita e referência t de `min(grupos espaciais, grupos temporais) - 1` graus de liberdade. O resultado registra hash do plano e das bases, parâmetros, tamanhos dos grupos e versões de Python, NumPy, SciPy e statsmodels. Para reprodução, utilize as mesmas versões registradas, entradas e ambiente numérico.

Faltas são excluídas por modelo e contadas. Amostra ou grupos insuficientes, posto deficiente, falha numérica ou covariância inválida retornam motivo sem coeficientes, intervalos ou p-valores. Mínimos quadrados é um ajuste direto, não um algoritmo iterativo que possa declarar convergência. Nenhum resultado é liberado automaticamente para publicação: ainda são necessários os diagnósticos de dependência entre blocos, influência e revisão institucional descritos em US21-T02. A associação não demonstra causalidade.

## Verificação

`python -m pytest -q tests/analises` inclui uma base sintética com coeficientes conhecidos de **2 segundos por 10 mm de chuva** e **0,5 segundo por km/h**, comparação numérica e repetição determinística. Também verifica plano bloqueado, integridade dos dados, amostra insuficiente, matriz singular, covariância inválida e falha numérica. Os aceites no teste são fictícios e não representam aprovação real. A implementação e os testes estão em `app/analises/coeficientes.py` e `tests/analises/test_coeficientes_us21.py`.

A tarefa permanece aguardando revisão por outra pessoa antes de ser movida para Pronto.

## Verificação dos intervalos de 95%

O teste `test_intervalos_95_contra_calculo_matricial_independente` confronta ambos os modelos com uma implementação matricial independente: soma as contribuições dos grupos espaciais e temporais e subtrai as interseções, com correção de amostra finita em cada componente. Usa o quantil tabelado `t(7) = 2,364624`, arredondado a seis casas. Os erros-padrão e os dois limites são comparados, e a execução é repetida para verificar reprodução.

Na base sintética do teste, o modelo secundário apresenta coeficiente de chuva 2,000000 e IC95% [1,244105; 2,755895]; para velocidade, coeficiente 0,500000 e IC95% [0,430308; 0,569692]. São resultados de ensaio, não dados reais nem validação de cobertura estatística.

Cada modelo calculado inclui limitações junto dos coeficientes, grupos e valor crítico. O registro também inclui o hash do mapa espacial, duração e origem dos blocos temporais, método, nível de confiança, correção, limites aprovados, hashes das bases e plano e versões das bibliotecas. Dependência residual entre blocos, influência e cobertura com poucos grupos continuam sujeitos a diagnóstico e revisão humana; os testes não demonstram que essas hipóteses sejam válidas para dados reais.
