# US14 — regras de separação das viagens observadas

Responsável sugerido: Lucas. Criada em 17/11/2026. Categoria: Dados.
Tarefa P, prioridade Alta. Sprint 2, ordem 12.

## Resultado

O módulo `app/viagens/separacao.py` separa observações tratadas válidas em
`ViagemObservada`. As regras abaixo são suficientes para criar os exemplos
reproduzíveis usados pela US14. A implementação não altera registros brutos nem
observações tratadas.

## Regras

1. Só `ObservacaoTratada.valida=True` inicia ou continua uma viagem.
2. Observações são ordenadas pelo horário do GPS dentro de cada prefixo.
3. Veículo, linha e sentido precisam permanecer iguais para haver continuidade.
4. Mudança de linha encerra a viagem anterior e inicia uma nova no primeiro
   ponto da nova linha.
5. Mudança de sentido encerra a viagem anterior e inicia uma nova no primeiro
   ponto do sentido seguinte.
6. Lacuna maior que 15 minutos encerra a viagem anterior e inicia uma nova
   quando a próxima observação válida aparecer.
7. Uma lacuna exatamente igual a 15 minutos não encerra a viagem.
8. Ao terminar o conjunto recebido, a última viagem é encerrada com
   `fim_da_amostra`; esse motivo não representa necessariamente o fim real da
   operação.

A lacuna padrão de 15 minutos é um limite operacional inicial, não uma decisão
de cobrança da US14. Deve ser confirmado pelo PO contra a cadência real da
API e pelo aceite dos exemplos de lacuna.

## Ambiguidades registradas para o PO

- A lacuna de 15 minutos corresponde ao intervalo máximo de referência definido
  pela implementação ou deve variar pela frequência da linha?
- Uma mudança aparente de sentido deve ser confirmada pela geometria da rota
  quando um veículo circular tiver apenas um sentido GTFS?
- GPS ausente ou inválido dentro de uma viagem deve encerrar a viagem ou só
  reduzir a cobertura observada?
- Uma mudança de linha no prefixo pode representar troca operacional ou dado
  inconsistente que exige revisão antes da separação?

Enquanto essas questões não tiverem resposta, os motivos ficam explícitos no
resultado e nenhuma viagem é criada a partir de observação inválida.

## Exemplos

| Caso | Observações | Resultado esperado |
| --- | --- | --- |
| Ida contínua | Sentido 0 nos minutos 0, 5 e 10 | Uma viagem iniciada em 0 e encerrada em 10 por fim da amostra |
| Volta | Sentido 0 nos minutos 0 e 5; sentido 1 nos minutos 10 e 15 | Uma viagem ida encerrada em 5 por mudança de sentido e uma volta encerrada em 15 |
| Lacuna | Válidas nos minutos 0, 5 e 21 | Duas viagens; a lacuna de 16 minutos encerra a primeira |
| Mudança de linha | Linha 8000-10 no minuto 0 e 8001-10 no minuto 5 | Duas viagens, uma por linha |
| Observação inválida | Válida no minuto 0, inválida no 5 e válida no 10 | Uma viagem do minuto 0 ao 10 |

Os exemplos fazem parte de `tests/indicadores/test_separacao_viagens.py`.

## Verificação

Comandos executados:

```powershell
python -m pytest -q tests/indicadores/test_separacao_viagens.py
git diff --check
```

Resultado: 6 testes passaram. `git diff --check` não encontrou erros.
