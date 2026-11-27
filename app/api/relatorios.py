"""Consulta pública dos relatórios US21 já revisados e persistidos."""
from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import erro_json, limitar_por_ip
from app.indicadores.propagacao import limites_mes
from app.relatorios.associacoes import consultar

relatorios_bp = Blueprint('relatorios', __name__)
limitar_por_ip(relatorios_bp, recurso='relatorios_associacoes',
               mensagem_excedido='Limite de consultas por minuto por IP atingido.')


@relatorios_bp.get('/relatorios/associacoes')
@relatorios_bp.get('/api/relatorios/associacoes')
def associacoes():
    for campo in request.args:
        if campo != 'mes' or len(request.args.getlist(campo)) != 1:
            return erro_json('PARAMETRO_INVALIDO', 'Informe somente um parâmetro mes.', 400, campo)
    mes = request.args.get('mes', '').strip()
    try:
        limites_mes(mes)
    except (ValueError, OverflowError):
        return erro_json('PARAMETRO_INVALIDO', 'Informe mes no formato AAAA-MM.', 400, 'mes')
    try:
        return consultar(mes)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error('Falha na consulta de associações (%s)', type(erro).__name__)
        return erro_json('CONSULTA_INDISPONIVEL', 'Não foi possível consultar o relatório.', 503)
