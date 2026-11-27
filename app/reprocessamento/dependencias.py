"""Dependências confirmadas entre resultados publicados.

Os demais cálculos compartilham entradas (viagens e passagens), mas não são
necessariamente derivados dos valores publicados uns pelos outros.
"""

DEPENDENTES = {
    "ESPERA_ESTIMADA": frozenset({"EXCESSO_ESPERA"}),
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
