# Review — US09-T03 e US32-T01/T02/T03

Data: 20/11/2026. Branch `US09US32`. Base e `origin/main`: `62414c2`.
Revisão técnica: Codex. Alterações sem commit, prontas para commit e Review.

## Correções desta revisão

| Problema | Correção e prova |
|---|---|
| Conferência interpretava um payload textual como JSON e podia esconder alteração de seu conteúdo literal. | Usa a mesma representação do exportador; textos permanecem literais. Teste de regressão altera um espaço dentro do payload textual e detecta a diferença. Conferência repetida nos 170.648 brutos reais, com resultado igual. |
| Parâmetros extras na URL de restauração poderiam sobrescrever host/base no driver, apesar da validação do endereço principal. | Permite apenas `charset=utf8mb4` na query. Rejeita `host`, `db` e `read_default_file` antes de abrir conexão. Testes de regressão e integração positiva com MySQL local. |

## Verificações

- Suíte completa: **482 passaram, 9 ignorados (opt-in), 14 subtestes passaram**.
- **3 integrações MySQL** executadas separadamente e aprovadas, incluindo
  snapshot sob INSERT concorrente, modo ANSI, geometria, tipos/nulos,
  arquivo original, recusa de base existente e referências órfãs.
- Verificação estática F/I/B e `git diff --check` passaram.
- Arquivos pendentes conferidos contra os segredos do `.env`: nenhum segredo
  encontrado; `.env` e dados de execução continuam ignorados pelo Git.
- Conferência real Parquet: conteúdo/metadados iguais; atraso inicial
  registrado corretamente, sem transformar código de falha em sucesso.
- Ensaio real anterior desta entrega: 21 tabelas e 27 FKs restauradas em
  40,5 s. Migrações posteriores testadas na base isolada, preservando hashes
  e conferindo `/health` e Alembic. Nenhuma alteração na base Aiven.

## Integração com a main

`git fetch origin` confirmou HEAD e `origin/main` no mesmo commit, sem
commits divergentes nem mudanças remotas pendentes. A maior parte da entrega
está em arquivos novos; alterações nos arquivos existentes foram mantidas
pequenas. Nenhum marcador de conflito deve acompanhar o commit.

Isso verifica a main disponível na revisão. Alterações posteriores da equipe
podem exigir nova sincronização antes do merge; não há promessa de ausência
de conflitos futuros.

## Aceite operacional

Implementação pronta para revisão. A publicação inicial real ocorreu após
06:00 e está registrada como atraso. A execução automática de uma madrugada
futura ainda não é uma observação realizada. A reprodução por outro
integrante da restauração é um passo do aceite descrito no cartão, separado
da revisão técnica aqui realizada.
