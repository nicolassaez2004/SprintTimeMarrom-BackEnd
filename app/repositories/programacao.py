"""Histórico de programação; cada captura e suas partidas são atômicas."""

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import ArquivoArmazenado, ImportacaoProgramacao, Linha, PartidaProgramada, VersaoGTFS


def obter_versao(versao_id):
    return db.session.get(VersaoGTFS, versao_id)


def obter_linha(versao_id, codigo):
    return db.session.get(Linha, (versao_id, codigo))


def consultar(versao_id, codigo, sentido, data_operacao, tipo_dia, versao_fonte=None,
              fonte="OFICIAL"):
    consulta = ImportacaoProgramacao.query.filter_by(
        versao_gtfs_id=versao_id, linha_codigo=codigo, sentido=sentido,
        data_operacao=data_operacao, tipo_dia=tipo_dia, fonte=fonte,
    )
    if versao_fonte is not None:
        consulta = consulta.filter_by(versao_fonte=versao_fonte)
    return consulta.order_by(ImportacaoProgramacao.verificada_em.desc(),
                             ImportacaoProgramacao.id.desc()).first()


def guardar(programacao, versao_id, digest, chave, fonte_url, tamanho, agora, *,
            fonte="OFICIAL", arquivo_existente=None, capturada_em=None):
    """Repetições atualizam a última verificação e preservam a primeira captura.

    A restrição única também protege contra dois processos inserindo o mesmo
    conteúdo. Um erro não relacionado à repetição é propagado após rollback.
    """
    def existente():
        return consultar(versao_id, programacao.linha, programacao.sentido,
                         programacao.data_operacao, programacao.tipo_dia, digest, fonte)

    def confirmar_repeticao(item):
        # Não regredir o horário caso uma consulta mais antiga termine depois.
        db.session.execute(db.update(ImportacaoProgramacao).where(
            ImportacaoProgramacao.id == item.id, ImportacaoProgramacao.verificada_em < agora,
        ).values(verificada_em=agora))
        db.session.commit()
        return item, False

    try:
        item = existente()
        if item is not None:
            return confirmar_repeticao(item)
        arquivo = arquivo_existente or ArquivoArmazenado.query.filter_by(chave=chave).one_or_none()
        if arquivo is None:
            arquivo = ArquivoArmazenado(
                chave=chave, nome_original="programacao-sptrans.json", mime_type="application/json",
                tamanho_bytes=tamanho, sha256=digest, criado_em=agora,
            )
            db.session.add(arquivo)
        item = ImportacaoProgramacao(
            versao_gtfs_id=versao_id, linha_codigo=programacao.linha,
            sentido=programacao.sentido, data_operacao=programacao.data_operacao,
            tipo_dia=programacao.tipo_dia, fonte=fonte, versao_fonte=digest,
            fonte_url=fonte_url, capturada_em=capturada_em or agora,
            verificada_em=agora, arquivo=arquivo,
        )
        item.partidas = [PartidaProgramada(
            ordem=ordem,
            saida_prevista=getattr(partida, "saida_prevista", partida),
            tipo_oferta=getattr(partida, "tipo_oferta", "FIXA"),
            intervalo_seg=getattr(partida, "intervalo_seg", None),
        ) for ordem, partida in enumerate(programacao.saidas, start=1)]
        db.session.add(item)
        db.session.commit()
        return item, True
    except IntegrityError:
        db.session.rollback()
        item = existente()
        if item is None:
            # Pode ter ocorrido disputa só pelo arquivo, compartilhado entre
            # datas/sentidos. Repetir a chamada externa é seguro; não esconder.
            raise
        return confirmar_repeticao(item)
    except Exception:
        db.session.rollback()
        raise
