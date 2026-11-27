# US30 — sondagem de API e painel

A migração `us30_0025` cria `verificacoes_disponibilidade`. Depois de aplicar
as migrações revisadas, execute em processo e máquina externos aos serviços,
com acesso à rede dos usuários e ao banco de monitoramento:

```sh
python -m scripts.sondagem --api https://exemplo/api/rota-real --painel https://exemplo/ --texto-api 'conteúdo esperado' --texto-painel 'conteúdo esperado'
```

Forneça URLs reais de consulta e da página. O processo faz requisições GET
independentes a cada 60 segundos, registra horário UTC, duração medida por
relógio monotônico, resultado e status HTTP. Respostas 2xx são consideradas
bem-sucedidas; os textos opcionais validam os primeiros 256 KiB do corpo.
Redirecionamentos contam como falha, portanto informe a URL final. Timeout
padrão é 5 segundos por alvo. `--uma-vez` executa apenas uma rodada.

A sondagem só mede acesso e, se configurado, o conteúdo textual; ela não prova
que todas as funções da API ou do painel operam. O banco precisa estar acessível
à máquina externa; se a persistência falhar, o processo registra erro no log e
não há buffer local, portanto lacunas na série não significam disponibilidade.
Não há provisionamento automático do processo ou painel de consulta nesta tarefa.

A verificação usa servidor HTTP local com falha alternada de cada alvo, conteúdo
inesperado, timeout e persistência SQLite. A migração foi exercitada isoladamente
em SQLite; a cadeia completa exige MySQL e deve ser revisada nesse ambiente.
Outra pessoa deve revisar antes de mover a tarefa para Pronto.
