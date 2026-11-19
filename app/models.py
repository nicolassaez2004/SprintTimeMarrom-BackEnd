"""Modelos de persistência da coleta GPS (US01).

Política de horário: todo instante é gravado em UTC. O MySQL `DATETIME` não
guarda fuso, então `UTCDateTime` converte valores com fuso para UTC antes de
gravar, rejeita valores sem fuso (ambíguos) e devolve sempre `datetime` com
`tzinfo=UTC` na leitura.

Alterações de esquema são feitas por migrações em `migrations/versions/`.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, Double, Index, UniqueConstraint, func
from sqlalchemy.dialects import mysql
from sqlalchemy.types import Text, TypeDecorator, UserDefinedType

from app import db


# BIGINT no MySQL; INTEGER no SQLite (usado nos testes) para ter autoincremento.
IdGrande = db.BigInteger().with_variant(db.Integer(), "sqlite")


def agora_utc():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """DATETIME(6) em UTC no MySQL; sempre timezone-aware no Python."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Horários devem ter fuso definido (use UTC)")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Veiculo(db.Model):
    """Ônibus da frota, identificado pelo prefixo (campo `p` da API Olho Vivo)."""

    __tablename__ = "veiculos"

    prefixo = db.Column(db.String(32), primary_key=True)


class OnibusSelecionado(db.Model):
    """Configuração: indica que um veículo deve ser monitorado pela coleta.

    Remover a seleção não afeta o histórico bruto, que aponta para `Veiculo`.
    """

    __tablename__ = "onibus_selecionados"

    prefixo = db.Column(
        db.String(32),
        db.ForeignKey(
            "veiculos.prefixo",
            ondelete="RESTRICT",
            name="fk_onibus_selecionados_veiculo",
        ),
        primary_key=True,
    )
    # A consulta de posições da SPTrans é feita por código de linha.
    codigo_linha = db.Column(db.Integer, nullable=False, index=True)

    veiculo = db.relationship("Veiculo")


ESTADOS_EXECUCAO = ("EM_ANDAMENTO", "OK", "PARCIAL", "FALHA")


class ExecucaoColeta(db.Model):
    """Um ciclo de coleta do BusCollector: horários, resultado e contagens.

    Ciclos gravados em uma janela = linhas desta tabela na janela; ciclos
    esperados = duração da janela / intervalo configurado. Não é `LacunaColeta`
    (o intervalo sem cobertura).
    """

    __tablename__ = "execucoes_coleta"
    __table_args__ = (
        CheckConstraint(
            "estado IN ('EM_ANDAMENTO', 'OK', 'PARCIAL', 'FALHA')",
            name="ck_execucoes_coleta_estado",
        ),
        Index("ix_execucoes_coleta_iniciada_em", "iniciada_em"),
    )

    id = db.Column(IdGrande, primary_key=True, autoincrement=True)
    fonte = db.Column(
        db.String(32), nullable=False, default="OLHO_VIVO", server_default="OLHO_VIVO"
    )
    # Instante em que o agendador planejou o ciclo; NULL em ciclos anteriores
    # à migração 0002 (valor desconhecido).
    prevista_em = db.Column(UTCDateTime, nullable=True)
    iniciada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    finalizada_em = db.Column(UTCDateTime, nullable=True)
    estado = db.Column(db.String(16), nullable=False, default="EM_ANDAMENTO")
    tentativas = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    # Posições dos ônibus selecionados recebidas no ciclo; NULL em ciclos
    # anteriores à migração 0002 (valor desconhecido).
    recebidos = db.Column(db.Integer, nullable=True)
    novos_persistidos = db.Column(db.Integer, nullable=False, default=0)
    erro = db.Column(db.Text, nullable=True)


class RegistroBrutoGPS(db.Model):
    """Posição bruta recebida da API Olho Vivo.

    `payload` guarda o objeto do veículo exatamente como a API devolveu. As
    colunas tipadas são uma leitura dele: valores ausentes ou inválidos ficam
    NULL nelas, mas o original continua no payload. A validação e a
    classificação de qualidade cabem a `ObservacaoTratada`.

    O registro aponta ao ciclo que o persistiu primeiro: a mesma posição
    (prefixo + `horario_gps`) não é gravada de novo quando reaparece.
    """

    __tablename__ = "registros_gps"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_registros_gps_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_registros_gps_longitude"),
        Index("ix_registros_gps_onibus_instante", "prefixo_origem", "coletado_em"),
        UniqueConstraint(
            "prefixo_origem", "horario_gps", name="uq_registros_gps_prefixo_horario"
        ),
    )

    id = db.Column(IdGrande, primary_key=True, autoincrement=True)
    execucao_id = db.Column(
        IdGrande,
        db.ForeignKey("execucoes_coleta.id", ondelete="CASCADE"),
        nullable=False,
    )
    veiculo_prefixo = db.Column(
        db.String(32),
        db.ForeignKey(
            "veiculos.prefixo", ondelete="RESTRICT", name="fk_registros_gps_veiculo"
        ),
        nullable=True,
    )
    # Valores como recebidos da fonte (diagrama: prefixoOrigem, linhaOrigem...).
    prefixo_origem = db.Column(db.String(32), nullable=True)
    linha_origem = db.Column(db.String(16), nullable=True)
    sentido_origem = db.Column(db.String(8), nullable=True)
    # DOUBLE: FLOAT do MySQL tem precisão simples, insuficiente para coordenadas.
    latitude = db.Column(Double, nullable=True)
    longitude = db.Column(Double, nullable=True)
    # Instante informado pela API para a posição (campo `ta`), em UTC.
    horario_gps = db.Column(UTCDateTime, nullable=True)
    # Instante da captura pelo coletor (início do ciclo), em UTC.
    coletado_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    payload = db.Column(db.JSON, nullable=True)

    veiculo = db.relationship("Veiculo", backref="registros_gps")
    execucao = db.relationship("ExecucaoColeta", backref="registros_gps")

    @property
    def completo(self):
        return None not in (self.latitude, self.longitude, self.horario_gps)

    def __repr__(self):
        return (
            f"<RegistroBrutoGPS prefixo={self.prefixo_origem!r} "
            f"coletado_em={self.coletado_em!r}>"
        )


class ArquivoArmazenado(db.Model):
    """Metadados de um arquivo imutável guardado pelo FileStore local."""

    __tablename__ = "arquivos_armazenados"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    chave = db.Column(db.String(255), nullable=False, unique=True)
    nome_original = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(128), nullable=False)
    tamanho_bytes = db.Column(db.BigInteger, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False, index=True)
    criado_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)


class VersaoGTFS(db.Model):
    """Captura versionada do feed; hash identifica o conteúdo do ZIP original."""

    __tablename__ = "versoes_gtfs"
    __table_args__ = (db.UniqueConstraint("hash", name="uq_versoes_gtfs_hash"),)

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    capturada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    hash = db.Column(db.String(64), nullable=False)
    vigencia_inicio = db.Column(db.Date, nullable=False)
    vigencia_fim = db.Column(db.Date, nullable=True)
    arquivo_id = db.Column(
        db.String(36),
        db.ForeignKey("arquivos_armazenados.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    arquivo = db.relationship("ArquivoArmazenado")


class _LinestringMySQL(UserDefinedType):
    cache_ok = True

    def __init__(self, externo=None):
        # Tipo do resultado de ST_AsText: o LineString, que converte o WKT.
        self.externo = externo

    def get_col_spec(self, **kw):
        return "LINESTRING"

    def bind_expression(self, valor):
        return func.ST_GeomFromText(valor)

    def column_expression(self, coluna):
        return func.ST_AsText(coluna, type_=self.externo)


class LineString(TypeDecorator):
    """Tipo espacial LINESTRING no MySQL (x = longitude, y = latitude).

    No Python é uma lista de pares [longitude, latitude]; trafega como WKT.
    Fora do MySQL (testes em SQLite) o WKT é guardado como texto.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(_LinestringMySQL(self))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, valor, dialect):
        if valor is None:
            return None
        if len(valor) < 2:
            raise ValueError("LineString precisa de ao menos dois pontos")
        return "LINESTRING(" + ", ".join(f"{x!r} {y!r}" for x, y in valor) + ")"

    def process_result_value(self, valor, dialect):
        if valor is None:
            return None
        pontos = valor[valor.index("(") + 1:valor.rindex(")")].split(",")
        return [[float(n) for n in ponto.split()] for ponto in pontos]


# Identificadores GTFS diferenciam maiúsculas e espaços; a collation padrão do
# MySQL não, então o código usa comparação binária.
CodigoGTFS = db.String(64).with_variant(
    mysql.VARCHAR(64, collation="utf8mb4_0900_bin"), "mysql"
)


def _fk_versao(tabela):
    return db.ForeignKey(
        "versoes_gtfs.id", ondelete="RESTRICT", name=f"fk_{tabela}_versao_gtfs"
    )


class Linha(db.Model):
    """Linha (`routes.txt`) de uma versão GTFS; `codigo` é o `route_id`.

    O mesmo código pode reaparecer em outra versão, por isso a chave é
    (versão, código).
    """

    __tablename__ = "linhas"

    versao_gtfs_id = db.Column(db.String(36), _fk_versao("linhas"), primary_key=True)
    codigo = db.Column(CodigoGTFS, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)

    versao_gtfs = db.relationship("VersaoGTFS")


class Parada(db.Model):
    """Parada (`stops.txt`) de uma versão GTFS; `codigo` é o `stop_id`."""

    __tablename__ = "paradas"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_paradas_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_paradas_longitude"),
    )

    versao_gtfs_id = db.Column(db.String(36), _fk_versao("paradas"), primary_key=True)
    codigo = db.Column(CodigoGTFS, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)
    # Decimal, como no diagrama: guarda os dígitos publicados (até 8 casas, ~1 mm).
    latitude = db.Column(db.Numeric(11, 8), nullable=False)
    longitude = db.Column(db.Numeric(11, 8), nullable=False)

    versao_gtfs = db.relationship("VersaoGTFS")


class Trecho(db.Model):
    """Par de paradas consecutivas de uma linha, num sentido e trajeto GTFS.

    `trajeto` (acréscimo ao diagrama) é o `trip_id` que representa uma
    sequência de paradas: a mesma linha e sentido podem ter sequências
    diferentes, e cada uma tem sua própria `ordem` a partir de 1. As chaves
    compostas obrigam linha e paradas a existirem na mesma versão GTFS.
    """

    __tablename__ = "trechos"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "linha_codigo"], ["linhas.versao_gtfs_id", "linhas.codigo"],
            name="fk_trechos_linha", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_origem_codigo"],
            ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_trechos_parada_origem", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_destino_codigo"],
            ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_trechos_parada_destino", ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "versao_gtfs_id", "linha_codigo", "trajeto", "ordem",
            name="uq_trechos_trajeto_ordem",
        ),
        CheckConstraint("ordem >= 1", name="ck_trechos_ordem"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    # A versão é garantida pela chave composta com `linhas`.
    versao_gtfs_id = db.Column(db.String(36), nullable=False)
    linha_codigo = db.Column(CodigoGTFS, nullable=False)
    # `direction_id` do GTFS (0 ida, 1 volta), como publicado.
    sentido = db.Column(db.String(8), nullable=False)
    trajeto = db.Column(CodigoGTFS, nullable=False)
    ordem = db.Column(db.Integer, nullable=False)
    parada_origem_codigo = db.Column(CodigoGTFS, nullable=False)
    parada_destino_codigo = db.Column(CodigoGTFS, nullable=False)
    geometria = db.Column(LineString, nullable=False)

    linha = db.relationship("Linha", overlaps="origem,destino")
    origem = db.relationship(
        "Parada", foreign_keys=[versao_gtfs_id, parada_origem_codigo],
        overlaps="linha,destino",
    )
    destino = db.relationship(
        "Parada", foreign_keys=[versao_gtfs_id, parada_destino_codigo],
        overlaps="linha,origem",
    )


class VerificacaoGTFS(db.Model):
    """Agenda, resultado e prazo de cada tentativa, inclusive após reiniciar."""

    __tablename__ = "verificacoes_gtfs"
    __table_args__ = (
        CheckConstraint("estado IN ('EM_ANDAMENTO', 'OK', 'PARCIAL', 'FALHA')",
                        name="ck_verificacoes_gtfs_estado"),
        Index("ix_verificacoes_gtfs_iniciada_em", "iniciada_em"),
        Index("ix_verificacoes_gtfs_hash_identificado", "hash_identificado"),
    )

    id = db.Column(IdGrande, primary_key=True, autoincrement=True)
    prevista_em = db.Column(UTCDateTime, nullable=False)
    iniciada_em = db.Column(UTCDateTime, nullable=False)
    finalizada_em = db.Column(UTCDateTime, nullable=True)
    estado = db.Column(db.String(16), nullable=False, default="EM_ANDAMENTO")
    resultado = db.Column(db.String(16), nullable=True)
    hash_identificado = db.Column(db.String(64), nullable=True)
    identificada_em = db.Column(UTCDateTime, nullable=True)
    persistida_em = db.Column(UTCDateTime, nullable=True)
    versao_id = db.Column(db.String(36), db.ForeignKey(
        "versoes_gtfs.id", name="fk_verificacoes_gtfs_versao", ondelete="RESTRICT",
    ), nullable=True)
    erro = db.Column(db.String(128), nullable=True)

    @property
    def tempo_ate_base_segundos(self):
        if self.identificada_em is None or self.persistida_em is None:
            return None
        return (self.persistida_em - self.identificada_em).total_seconds()


class ObservacaoTratada(db.Model):
    """Resultado de tratamento separado da posição bruta imutável (US12).

    Visão tratada atual, com cardinalidade de no máximo uma observação por
    bruto como no diagrama. Resultados históricos de cada reprocessamento ficam
    em `ResultadoReprocessamento`, sem multiplicar esta relação. A linha usa
    a chave composta da rede GTFS; a viagem será ligada na sua história.
    Validade NULL significa validação pendente, sem contá-la como inválida ou
    incluí-la no denominador da associação.
    """

    __tablename__ = "observacoes_tratadas"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "linha_codigo"], ["linhas.versao_gtfs_id", "linhas.codigo"],
            name="fk_observacoes_linha_versionada", ondelete="RESTRICT"),
        db.ForeignKeyConstraint(
            ['correspondencia_id','versao_gtfs_id'],
            ['correspondencias_codigos.id','correspondencias_codigos.versao_gtfs_id'],
            name='fk_observacoes_correspondencia',ondelete='RESTRICT'),
        UniqueConstraint("bruto_id", name="uq_observacoes_tratadas_bruto"),
        CheckConstraint("LENGTH(TRIM(versao_correspondencia)) > 0",
                        name="ck_observacoes_tratadas_versao"),
        # Herda charset/collation da base, assim como versoes_gtfs (0003),
        # para manter as FKs textuais compatíveis nos ambientes existentes.
        {"mysql_engine": "InnoDB"},
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    bruto_id = db.Column(
        IdGrande,
        db.ForeignKey("registros_gps.id", name="fk_observacoes_tratadas_bruto",
                      ondelete="RESTRICT"),
        nullable=False,
    )
    # NULL: validação espacial pendente (sem GTFS, traçado ou limite municipal).
    valida = db.Column(db.Boolean, nullable=True)
    motivo_invalidez = db.Column(db.Text, nullable=True)
    versao_correspondencia = db.Column(db.String(128), nullable=False)
    versao_gtfs_id = db.Column(
        db.String(36),
        db.ForeignKey("versoes_gtfs.id", name="fk_observacoes_tratadas_gtfs",
                      ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    bruto = db.relationship(
        "RegistroBrutoGPS",
        backref=db.backref("observacao_tratada", uselist=False, passive_deletes="all"),
    )
    versao_gtfs = db.relationship("VersaoGTFS")
    linha_codigo = db.Column(CodigoGTFS, nullable=True)
    sentido_gtfs = db.Column(db.String(8), nullable=True)
    correspondencia_id = db.Column(db.String(36), nullable=True)
    motivo_correspondencia = db.Column(db.String(64), nullable=True)
    candidatos_correspondencia = db.Column(db.JSON, nullable=True)
    distancia_tracado_m = db.Column(db.Float, nullable=True)
    versao_validacao = db.Column(db.String(128), nullable=True)
    processada_em = db.Column(UTCDateTime, nullable=True)
    linha = db.relationship("Linha", overlaps="versao_gtfs")


class TabelaCorrespondencia(db.Model):
    """Conjunto imutável de vínculos conferidos, por GTFS e vigência inclusiva."""
    __tablename__ = 'tabelas_correspondencia'
    __table_args__ = (
        UniqueConstraint('id', 'versao_gtfs_id', name='uq_tabela_correspondencia_gtfs'),
        db.ForeignKeyConstraint(['substitui_id', 'versao_gtfs_id'],
            ['tabelas_correspondencia.id', 'tabelas_correspondencia.versao_gtfs_id'],
            name='fk_tabela_correspondencia_anterior', ondelete='RESTRICT'),
        CheckConstraint('substitui_id IS NULL OR substitui_id <> id',
                        name='ck_tabela_correspondencia_nao_auto'),
        CheckConstraint('vigencia_fim IS NULL OR vigencia_fim >= vigencia_inicio',
                        name='ck_tabela_correspondencia_vigencia'),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    versao = db.Column(db.String(96), nullable=False, unique=True)
    versao_gtfs_id = db.Column(db.String(36), db.ForeignKey('versoes_gtfs.id',
        name='fk_tabela_correspondencia_gtfs', ondelete='RESTRICT'), nullable=False)
    vigencia_inicio = db.Column(db.Date, nullable=False)
    vigencia_fim = db.Column(db.Date, nullable=True)
    fonte = db.Column(db.String(255), nullable=False)
    hash = db.Column(db.String(64), nullable=False)
    criada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    substitui_id = db.Column(db.String(36), nullable=True)


class CorrespondenciaCodigo(db.Model):
    __tablename__ = 'correspondencias_codigos'
    __table_args__ = (
        UniqueConstraint('id','versao_gtfs_id',name='uq_correspondencia_id_gtfs'),
        db.ForeignKeyConstraint(['tabela_id', 'versao_gtfs_id'],
            ['tabelas_correspondencia.id', 'tabelas_correspondencia.versao_gtfs_id'],
            name='fk_correspondencia_tabela_gtfs', ondelete='RESTRICT'),
        db.ForeignKeyConstraint(['versao_gtfs_id', 'linha_codigo'],
            ['linhas.versao_gtfs_id', 'linhas.codigo'],
            name='fk_correspondencia_linha', ondelete='RESTRICT'),
        CheckConstraint("sentido_olho_vivo IN ('1','2')", name='ck_correspondencia_sentido_ov'),
        CheckConstraint("sentido_gtfs IS NULL OR sentido_gtfs IN ('0','1')", name='ck_correspondencia_sentido_gtfs'),
        CheckConstraint('(linha_codigo IS NULL AND sentido_gtfs IS NULL) OR '
                        '(linha_codigo IS NOT NULL AND sentido_gtfs IS NOT NULL)', name='ck_correspondencia_destino'),
        Index('ix_correspondencia_origem', 'codigo_olho_vivo', 'sentido_olho_vivo'),
    )
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    tabela_id = db.Column(db.String(36), nullable=False)
    versao_gtfs_id = db.Column(db.String(36), nullable=False)
    codigo_olho_vivo = db.Column(db.String(32), nullable=False)
    sentido_olho_vivo = db.Column(db.String(8), nullable=False)
    linha_codigo = db.Column(CodigoGTFS, nullable=True)
    sentido_gtfs = db.Column(db.String(8), nullable=True)
    motivo = db.Column(db.String(255), nullable=True)


class ImportacaoProgramacao(db.Model):
    """Captura versionada, inclusive vazia, da programação de uma linha/dia.

    Extensão operacional ao diagrama: guarda a procedência comum às partidas
    e permite distinguir zero partidas oficiais de ausência de importação.
    """

    __tablename__ = "importacoes_programacao"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "linha_codigo"], ["linhas.versao_gtfs_id", "linhas.codigo"],
            name="fk_importacoes_programacao_linha", ondelete="RESTRICT",
        ),
        UniqueConstraint("versao_gtfs_id", "linha_codigo", "data_operacao", "sentido",
                         "tipo_dia", "fonte", "versao_fonte", name="uq_importacoes_programacao_contexto"),
        CheckConstraint("sentido IN ('0', '1')", name="ck_importacoes_programacao_sentido"),
        CheckConstraint("tipo_dia IN ('UTIL', 'SABADO', 'DOMINGO', 'FERIADO')",
                        name="ck_importacoes_programacao_tipo_dia"),
        CheckConstraint("fonte IN ('OFICIAL', 'GTFS')", name="ck_importacoes_programacao_fonte"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    versao_gtfs_id = db.Column(db.String(36), nullable=False)
    linha_codigo = db.Column(CodigoGTFS, nullable=False)
    data_operacao = db.Column(db.Date, nullable=False)
    sentido = db.Column(db.String(1), nullable=False)
    tipo_dia = db.Column(db.String(12), nullable=False)
    fonte = db.Column(db.String(8), nullable=False)
    versao_fonte = db.Column(db.String(64), nullable=False)
    fonte_url = db.Column(db.String(512), nullable=False)
    capturada_em = db.Column(UTCDateTime, nullable=False)
    verificada_em = db.Column(UTCDateTime, nullable=False)
    arquivo_id = db.Column(db.String(36), db.ForeignKey(
        "arquivos_armazenados.id", name="fk_importacoes_programacao_arquivo", ondelete="RESTRICT",
    ), nullable=False)

    linha = db.relationship("Linha")
    arquivo = db.relationship("ArquivoArmazenado")
    partidas = db.relationship("PartidaProgramada", back_populates="importacao",
                               order_by="PartidaProgramada.ordem", passive_deletes="all")


class PartidaProgramada(db.Model):
    """Saída com contexto/procedência herdados da captura, sem duplicar colunas.

    A ordem preserva partidas simultâneas da fonte. O sentido segue o GTFS:
    0 = ida, 1 = volta; não recebe diretamente os códigos 1/2 do Olho Vivo.
    """

    __tablename__ = "partidas_programadas"
    __table_args__ = (
        UniqueConstraint("importacao_id", "ordem", name="uq_partidas_programadas_ordem"),
        CheckConstraint("ordem >= 1", name="ck_partidas_programadas_ordem"),
        CheckConstraint("tipo_oferta IN ('FIXA', 'FREQUENCIA')", name="ck_partidas_programadas_tipo"),
        CheckConstraint("(tipo_oferta = 'FIXA' AND intervalo_seg IS NULL) OR "
                        "(tipo_oferta = 'FREQUENCIA' AND intervalo_seg IS NOT NULL AND intervalo_seg > 0)",
                        name="ck_partidas_programadas_intervalo"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    importacao_id = db.Column(db.String(36), db.ForeignKey(
        "importacoes_programacao.id", name="fk_partidas_programadas_importacao", ondelete="RESTRICT",
    ), nullable=False)
    ordem = db.Column(db.Integer, nullable=False)
    saida_prevista = db.Column(UTCDateTime, nullable=False)
    tipo_oferta = db.Column(db.String(12), nullable=False, default="FIXA")
    intervalo_seg = db.Column(db.Integer, nullable=True)
    importacao = db.relationship("ImportacaoProgramacao", back_populates="partidas")

    @property
    def linha(self):
        return self.importacao.linha

    @property
    def data_operacao(self):
        return self.importacao.data_operacao

    @property
    def sentido(self):
        return self.importacao.sentido

    @property
    def fonte(self):
        return self.importacao.fonte

    @property
    def versao_fonte(self):
        return self.importacao.versao_fonte

    @property
    def versao_gtfs(self):
        return self.importacao.linha.versao_gtfs

class ExecucaoProcessamento(db.Model):
    """Job durável e versionado, compatível com o contrato do diagrama."""

    __tablename__ = "execucoes_processamento"
    __table_args__ = (
        CheckConstraint(
            "estado IN ('AGUARDANDO', 'PROCESSANDO', 'PRONTO', 'ERRO')",
            name="ck_execucoes_processamento_estado",
        ),
        CheckConstraint("periodo_fim > periodo_inicio",
                        name="ck_execucoes_processamento_periodo"),
        CheckConstraint("tentativas >= 0",
                        name="ck_execucoes_processamento_tentativas"),
        CheckConstraint(
            "(reserva_token IS NULL AND reserva_ate IS NULL) OR "
            "(reserva_token IS NOT NULL AND reserva_ate IS NOT NULL)",
            name="ck_execucoes_processamento_reserva",
        ),
        UniqueConstraint("chave_logica", name="uq_execucoes_processamento_chave_logica"),
        Index("ix_execucoes_processamento_periodo", "periodo_inicio", "periodo_fim"),
        {"mysql_engine": "InnoDB"},
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    tipo = db.Column(db.String(32), nullable=False, default="REPROCESSAMENTO")
    chave_logica = db.Column(
        db.String(255), nullable=False, default=lambda: str(uuid4())
    )
    parametros = db.Column(db.JSON, nullable=False, default=dict)
    estado = db.Column(db.String(16), nullable=False, default="AGUARDANDO")
    periodo_inicio = db.Column(UTCDateTime, nullable=False)
    periodo_fim = db.Column(UTCDateTime, nullable=False)
    # Snapshot fechado: novos brutos com coleta retroativa não entram neste manifesto.
    bruto_id_maximo = db.Column(IdGrande, nullable=False)
    versao_base = db.Column(db.String(128), nullable=False)
    versao_gtfs_id = db.Column(
        db.String(36),
        db.ForeignKey("versoes_gtfs.id", name="fk_execucoes_processamento_gtfs",
                      ondelete="RESTRICT"),
        nullable=True,
    )
    versao_codigo = db.Column(db.String(128), nullable=False)
    versao_regra = db.Column(db.String(128), nullable=False)
    tentativas = db.Column(db.Integer, nullable=False, default=0)
    criada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    iniciada_em = db.Column(UTCDateTime, nullable=True)
    finalizada_em = db.Column(UTCDateTime, nullable=True)
    reserva_token = db.Column(db.String(36), nullable=True)
    reserva_ate = db.Column(UTCDateTime, nullable=True)
    erro = db.Column(db.String(128), nullable=True)

    versao_gtfs = db.relationship("VersaoGTFS")
    resultados_reprocessamento = db.relationship(
        "ResultadoReprocessamento", back_populates="execucao_processamento"
    )


class ResultadoReprocessamento(db.Model):
    """Snapshot versionado de um resultado produzido por uma execução."""

    __tablename__ = "resultados_reprocessamento"
    __table_args__ = (
        UniqueConstraint(
            "execucao_processamento_id", "bruto_id",
            name="uq_resultados_reprocessamento_execucao_bruto",
        ),
        CheckConstraint("LENGTH(TRIM(versao_correspondencia)) > 0",
                        name="ck_resultados_reprocessamento_versao"),
        {"mysql_engine": "InnoDB"},
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    execucao_processamento_id = db.Column(
        db.String(36),
        db.ForeignKey("execucoes_processamento.id",
                      name="fk_resultados_reprocessamento_execucao", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    bruto_id = db.Column(
        IdGrande,
        db.ForeignKey("registros_gps.id", name="fk_resultados_reprocessamento_bruto",
                      ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    valida = db.Column(db.Boolean, nullable=False)
    motivo_invalidez = db.Column(db.Text, nullable=True)
    versao_correspondencia = db.Column(db.String(128), nullable=False)
    versao_gtfs_id = db.Column(
        db.String(36),
        db.ForeignKey("versoes_gtfs.id", name="fk_resultados_reprocessamento_gtfs",
                      ondelete="RESTRICT"),
        nullable=True,
    )

    execucao_processamento = db.relationship(
        "ExecucaoProcessamento", back_populates="resultados_reprocessamento"
    )
    bruto = db.relationship("RegistroBrutoGPS")
    versao_gtfs = db.relationship("VersaoGTFS")
