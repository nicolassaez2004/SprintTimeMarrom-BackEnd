# US02 — Vigência das versões GTFS sem `feed_info.txt`

Defeito encontrado na revisão do site (27/11/2026). O feed da SPTrans não traz `feed_info.txt`, então toda
versão nascia com vigência aberta (`vigencia_fim` NULL) e nada encerrava a anterior. A partir da segunda
captura, todo dia virava `VersaoGTFSAmbigua`: nenhuma observação era ligada a uma versão (US03) e a
programação e a classificação da rede ficavam bloqueadas (US04, US06, US07).

## Correção ([app/gtfs/service.py](../app/gtfs/service.py))

- `encerrar_vigencias_abertas(nova)`: na mesma transação da captura, cada versão de **vigência aberta**
  que começou até o início da nova termina na véspera desse início. Vigências explícitas (`feed_info.txt`)
  nunca mudam: uma sobreposição entre elas continua ambígua, de propósito. Uma captura mais antiga não
  encerra uma mais nova.
- O início sem `feed_info.txt` passa a ser o **dia da captura em São Paulo**. Antes era a data em UTC, e
  capturas depois das 21:00 caíam no dia seguinte.
- Bases já gravadas: `python -m app.gtfs corrigir-vigencias` aplica a mesma regra em ordem de captura. É
  idempotente.

Testes em [tests/gtfs/test_vigencia.py](../tests/gtfs/test_vigencia.py):
- duas capturas sem `feed_info` não ficam ambíguas, e a véspera da nova ainda é da antiga;
- dia de São Paulo às 23:30;
- vigência explícita preservada;
- correção de base antiga, idempotente.

Suíte completa: 1404 aprovados. A única falha, `test_falha_sql_preserva_objeto_e_permite_retomada`, é
intermitente e anterior a esta mudança: também falha na `main`, porque o ZIP montado no teste leva o
horário atual.
