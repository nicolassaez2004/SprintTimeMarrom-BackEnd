"""Caixa interna e métricas: python -m app.monitoramento --help."""

import argparse
import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app import create_app, db
from app.coleta.resiliencia import diretorio_journal
from app.integrations.alertas import AlertaEntregaError, AlertaWebhook
from app.models import NotificacaoColeta
from app.repositories.coleta import listar_alertas

from .journal import publicar
from .metricas import medir_gravacao


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser(
        "alertas",
        help="Lê a caixa compartilhada MySQL e usa a caixa local durante falhas",
    )
    sub.add_parser(
        "teste-canal",
        help="Entrega uma mensagem TESTE ao canal configurado, sem incidente ou GPS",
    )
    met = sub.add_parser("metricas")
    met.add_argument("--inicio", required=True, type=datetime.fromisoformat)
    met.add_argument("--fim", required=True, type=datetime.fromisoformat)
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        try:
            if args.comando == "alertas":
                pasta = diretorio_journal() / "alertas"
                locais = [
                    json.loads(p.read_text(encoding="utf-8"))
                    for p in pasta.glob("*.json")
                ]
                por_id = {a["id"]: a for a in locais}
                sql_disponivel = True
                try:
                    for alerta in listar_alertas():
                        por_id.setdefault(alerta["id"], alerta)
                except SQLAlchemyError:
                    db.session.rollback()
                    sql_disponivel = False
                dados = sorted(
                    por_id.values(), key=lambda a: (a["criada_em"], a["tipo"])
                )
                print(
                    json.dumps(
                        dict(
                            canal="CAIXA_INTERNA",
                            diretorio=str(pasta),
                            sql_disponivel=sql_disponivel,
                            alertas=dados,
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.comando == "teste-canal":
                agora = datetime.now(timezone.utc)
                id = str(uuid4())
                url = app.config.get("ALERTA_WEBHOOK_URL")
                mensagem = dict(
                    id=id,
                    lacuna_id=None,
                    tipo="TESTE",
                    simulado=True,
                    criada_em=agora.isoformat(),
                    enviada_em=agora.isoformat(),
                    canal="WEBHOOK" if url else "CAIXA_INTERNA",
                    mensagem="TESTE US29: canal de alertas funcionando. Não representa falha real da coleta.",
                )
                if url:
                    try:
                        AlertaWebhook(url)(mensagem)
                        mensagem["enviada_em"] = datetime.now(timezone.utc).isoformat()
                    except AlertaEntregaError as erro:
                        # Registrado como não entregue; a saída não tem a URL.
                        mensagem["enviada_em"] = None
                        mensagem["erro_entrega"] = str(erro)
                publicar(diretorio_journal() / "alertas" / (id + ".json"), mensagem)
                db.session.add(
                    NotificacaoColeta(
                        id=id,
                        lacuna_id=None,
                        tipo="TESTE",
                        criada_em=agora,
                        enviada_em=(
                            datetime.fromisoformat(mensagem["enviada_em"])
                            if mensagem["enviada_em"]
                            else None
                        ),
                        canal=mensagem["canal"],
                    )
                )
                db.session.commit()
                print(json.dumps(mensagem, ensure_ascii=False, indent=2))
                if mensagem["enviada_em"] is None:
                    raise SystemExit(1)
            else:
                print(
                    json.dumps(
                        medir_gravacao(
                            args.inicio,
                            args.fim,
                            app.config["GPS_COLLECTION_INTERVAL_SECONDS"],
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        finally:
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    main()
