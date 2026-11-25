# US18-T03B — registro da conferência e do aceite

**Status: EM REVISÃO (aceite institucional pendente).** Os vinte casos foram
comparados em 24/11/2026 e reconferidos na review de 25/11/2026. A dispensa do
envio, registrada antes pelo responsável, não substitui o critério de aceite do
requisito ("confere com o cálculo manual em 20 casos de teste aprovados pela
Instituição Parceira"); por isso a tarefa não é declarada pronta.

Revisão de 25/11/2026: o caso C12 ("dia com duas passagens", que repetia a
insuficiência do C05) foi substituído por **operação após a meia-noite**
(horário GTFS > 24h), que faltava na cobertura exigida.

Origem dos casos: **SINTÉTICA CONTROLADA**. São 5 casos por escala (15 min,
faixa, dia e mês):
- [caderno](US18-T03B-casos.json), com as contas de cada caso;
- [relatório](../evidencias/US18-T03B-conferencia.json), com as respostas da API.

Fórmulas: espera estimada E = Σh² ÷ (2·Σh); excesso = E − E_prog, com
E_prog = ΣH² ÷ (2·ΣH) dos intervalos programados vigentes (H ÷ 2 com intervalo
único); excesso negativo mantido; menos de três passagens ou Σh inválida → sem
resultado.

A regra E_prog = ΣH² ÷ (2·ΣH) para janelas que cruzam faixas (caso C04) foi
escolhida pela equipe e **aguarda decisão do PO**.

| Campo | Registro |
| --- | --- |
| Conferência técnica | 20/20 casos conferem (`CONFERIU_EXEMPLO`), SQLite em memória, dados sintéticos |
| Envio à Instituição Parceira | **Não enviado** |
| Aceite da Instituição Parceira | **Pendente** |
| Situação do cartão | Em revisão: o critério comum US14–US19 exige os 20 casos aprovados pela Instituição Parceira |

Nada foi enviado à Instituição Parceira e não há aprovação registrada.
