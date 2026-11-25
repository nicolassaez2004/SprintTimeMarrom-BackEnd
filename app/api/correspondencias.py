from datetime import date
import re
from flask import Blueprint, request
from .relatorio import relatorio_diario

qualidade_bp=Blueprint('qualidade_correspondencias',__name__)


@qualidade_bp.get('/qualidade/correspondencias')
@qualidade_bp.get('/api/qualidade/correspondencias')
def qualidade_correspondencias():
    raw=request.args.get('data','')
    limite=request.args.get('amostra','20')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw) or not re.fullmatch(r'\d{1,3}',limite):
        return {'erro':'Informe data=YYYY-MM-DD e amostra entre 0 e 100'},400
    try:
        dia=date.fromisoformat(raw)
        return relatorio_diario(dia,int(limite))
    except ValueError:
        return {'erro':'Data inválida ou amostra fora de 0 a 100'},400
