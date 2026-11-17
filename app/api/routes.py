from flask import Blueprint, request

# Blueprint com as rotas da API
api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/exemplo")
def exemplo():
    """Rota de teste: GET /api/exemplo?linha=8000"""

    linha = request.args.get("linha", "").strip()

    # Parametro ausente ou nao numerico
    if not linha.isdigit():
        return {
            "erro": "Parametro 'linha' obrigatorio e deve ser numerico"
        }, 400

    return {"linha": int(linha)}, 200
