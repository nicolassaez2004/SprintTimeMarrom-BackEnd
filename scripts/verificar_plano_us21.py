"""Verifica plano e dados da US21 sem calcular associações ou registrar aprovações."""
import argparse
import json
from pathlib import Path

from app.analises.plano import verificar_prontidao

RAIZ=Path(__file__).resolve().parents[1]


def executar(plano=None,bases=None):
    plano=plano or RAIZ/'docs/analises/US21-plano-v1.json'
    bases=bases or [RAIZ/'docs/validacoes/US21-T01-amostra.json']
    return verificar_prontidao(json.loads(Path(plano).read_text(encoding='utf-8')),
        [json.loads(Path(b).read_text(encoding='utf-8')) for b in bases])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plano',type=Path)
    p.add_argument('--base',type=Path,action='append')
    p.add_argument('--saida',type=Path)
    a=p.parse_args(); r=executar(a.plano,a.base)
    if a.saida:
        a.saida.parent.mkdir(parents=True,exist_ok=True)
        a.saida.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(r,ensure_ascii=False))
