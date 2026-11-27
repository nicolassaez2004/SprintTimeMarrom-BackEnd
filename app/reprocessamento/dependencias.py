"""Dependências confirmadas entre resultados publicados.

Os demais cálculos compartilham entradas (viagens e passagens), mas não são
necessariamente derivados dos valores publicados uns pelos outros.

- Excesso de espera é calculado a partir da espera estimada (US18).
- O agregado mensal da US19-T03 resume as variações por trecho da US19-T01.
"""

DEPENDENTES = {
    "ESPERA_ESTIMADA": frozenset({"EXCESSO_ESPERA"}),
    "VARIACAO_ATRASO_TRECHO": frozenset({"AUMENTO_ATRASO_TRECHO_MENSAL"}),
}


def incluir_dependentes(indicadores):
    encontrados = set(indicadores)
    pendentes = list(encontrados)
    while pendentes:
        for dependente in DEPENDENTES.get(pendentes.pop(), ()):
            if dependente not in encontrados:
                encontrados.add(dependente)
                pendentes.append(dependente)
    return encontrados
