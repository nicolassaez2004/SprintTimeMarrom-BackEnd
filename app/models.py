from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Index

from app import db


class OnibusSelecionado(db.Model):
    """Ônibus SPTrans incluído na seleção persistida pela aplicação."""

    __tablename__ = "onibus_selecionados"

    # A API identifica o veículo pelo prefixo (campo `p`).
    prefixo = db.Column(db.String(32), primary_key=True)
    # A consulta de posições da SPTrans é feita por código de linha.
    codigo_linha = db.Column(db.Integer, nullable=False, index=True)


class ExecucaoColeta(db.Model):
    """Resumo de um ciclo de consulta e gravação de posições."""

    __tablename__ = "execucoes_coleta"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    iniciada_em = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    finalizada_em = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(16), nullable=False, default="executando")
    quantidade_registros = db.Column(db.Integer, nullable=False, default=0)
    erro = db.Column(db.Text, nullable=True)


class RegistroGPS(db.Model):
    """Uma posição observada de um ônibus selecionado em um instante."""

    __tablename__ = "registros_gps"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_registros_gps_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_registros_gps_longitude"),
        Index("ix_registros_gps_onibus_instante", "prefixo_onibus", "registrado_em"),
    )

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    execucao_id = db.Column(
        db.BigInteger,
        db.ForeignKey("execucoes_coleta.id", ondelete="CASCADE"),
        nullable=False,
    )
    prefixo_onibus = db.Column(
        db.String(32),
        db.ForeignKey("onibus_selecionados.prefixo", ondelete="RESTRICT"),
        nullable=False,
    )
    # `cl` retornado pela SPTrans é o código da linha, não o identificador do ônibus.
    codigo_linha = db.Column(db.Integer, nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    # Instante da captura, armazenado em UTC; cada linha representa uma amostra.
    registrado_em = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    onibus = db.relationship("OnibusSelecionado", backref="registros_gps")
    execucao = db.relationship("ExecucaoColeta", backref="registros_gps")

    def __repr__(self):
        return (
            f"<RegistroGPS prefixo={self.prefixo_onibus!r} "
            f"registrado_em={self.registrado_em!r}>"
        )

# Nomes usados no diagrama de classes/contrato de gravação da US12.
Veiculo = OnibusSelecionado
RegistroBrutoGPS = RegistroGPS
