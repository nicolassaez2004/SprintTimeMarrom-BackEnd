"""Modelos de persistência da coleta GPS (US01).

Política de horário: todo instante é gravado em UTC. O MySQL `DATETIME` não
guarda fuso, então `UTCDateTime` converte valores com fuso para UTC antes de
gravar, rejeita valores sem fuso (ambíguos) e devolve sempre `datetime` com
`tzinfo=UTC` na leitura.

Alterações de esquema são feitas por migrações em `migrations/versions/`.
"""

from datetime import datetime, timedelta, timezone
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
    chave_logica = db.Column(db.String(36), nullable=True, unique=True)
    lacuna_id = db.Column(db.String(36), db.ForeignKey('lacunas_coleta.id',
        name='fk_execucao_coleta_lacuna', ondelete='RESTRICT'), nullable=True)
    confirmada_em = db.Column(UTCDateTime, nullable=True)
    duracao_gravacao_ms = db.Column(db.BigInteger, nullable=True)


class LacunaColeta(db.Model):
    """Intervalo sem cobertura; pode incluir vários ciclos ou processo parado."""

    __tablename__ = "lacunas_coleta"
    __table_args__ = (
        CheckConstraint("fim IS NULL OR fim >= inicio", name="ck_lacuna_intervalo"),
    )
    id = db.Column(db.String(36), primary_key=True)
    inicio = db.Column(UTCDateTime, nullable=False)
    fim = db.Column(UTCDateTime, nullable=True)
    causa = db.Column(db.String(128), nullable=False)
    fonte = db.Column(db.String(32), nullable=False)
    aberta_fonte = db.Column(db.String(32), nullable=True, unique=True)


class EstadoColeta(db.Model):
    """Posse técnica: token confere exclusão também na transação de gravação."""

    __tablename__ = "estados_coleta"
    fonte = db.Column(db.String(32), primary_key=True)
    token = db.Column(db.String(36), nullable=False)


class NotificacaoColeta(db.Model):
    """Uma notificação lógica por incidente/tipo, entregue à caixa interna."""

    __tablename__ = "notificacoes_coleta"
    __table_args__ = (
        UniqueConstraint("lacuna_id", "tipo", name="uq_notificacao_lacuna_tipo"),
    )
    id = db.Column(db.String(36), primary_key=True)
    lacuna_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "lacunas_coleta.id", name="fk_notificacao_lacuna", ondelete="RESTRICT"
        ),
        nullable=True,
    )
    tipo = db.Column(db.String(16), nullable=False)
    criada_em = db.Column(UTCDateTime, nullable=False)
    enviada_em = db.Column(UTCDateTime, nullable=True)
    canal = db.Column(db.String(32), nullable=False)


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
        # Exportação diária (US09): faixa por dia e paginação keyset.
        Index("ix_registros_gps_coletado_em_id", "coletado_em", "id"),
        # Extração por linha (US28): mesma paginação keyset, só nas linhas pedidas.
        Index("ix_registros_gps_linha_coletado_em_id", "linha_origem", "coletado_em", "id"),
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
    # Início da consulta que produziu esta resposta, em UTC. Pode ser posterior
    # ao início do ciclo quando houve retentativas; ExecucaoColeta guarda ambos.
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
        UniqueConstraint("id", "versao_gtfs_id", "linha_codigo",
                         name="uq_trechos_id_versao_linha"),
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


class VersaoInfraestrutura(db.Model):
    """Origem imutável de uma importação de corredores/faixas."""

    __tablename__ = "versoes_infraestrutura"
    __table_args__ = (
        UniqueConstraint("fonte", "sha256", name="uq_versao_infra_fonte_hash"),
        CheckConstraint("crs_origem <> ''", name="ck_versao_infra_crs"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    fonte = db.Column(db.String(64), nullable=False)
    url_fonte = db.Column(db.String(1024), nullable=False)
    versao_fonte = db.Column(db.String(128), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    crs_origem = db.Column(db.String(32), nullable=False)
    capturada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    metadados = db.Column(db.JSON, nullable=False, default=dict)


class AtualizacaoMensalInfraestrutura(db.Model):
    """Recibo mensal da tentativa de atualização das fontes de infraestrutura."""

    __tablename__ = "atualizacoes_mensais_infraestrutura"
    __table_args__ = (CheckConstraint(
        "estado IN ('EM_ANDAMENTO', 'OK', 'FALHA')", name="ck_atualizacao_mensal_infra_estado",
    ),)
    mes = db.Column(db.String(7), primary_key=True)  # YYYY-MM
    iniciada_em = db.Column(UTCDateTime, nullable=False)
    finalizada_em = db.Column(UTCDateTime, nullable=True)
    estado = db.Column(db.String(16), nullable=False)
    resultado = db.Column(db.JSON, nullable=False, default=dict)


class ClassificacaoInfraestrutura(db.Model):
    """Feição normalizada de corredor/faixa, vinculada à sua versão de origem."""

    __tablename__ = "classificacoes_infraestrutura"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["versao_id"], ["versoes_infraestrutura.id"],
            name="fk_classificacao_infra_versao", ondelete="RESTRICT",
        ),
        UniqueConstraint("versao_id", "identificador_fonte", name="uq_classificacao_infra_origem"),
        CheckConstraint("categoria IN ('CORREDOR', 'FAIXA_EXCLUSIVA')", name="ck_classificacao_infra_categoria"),
        Index("ix_classificacao_infra_versao_categoria", "versao_id", "categoria"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    versao_id = db.Column(db.String(36), nullable=False)
    identificador_fonte = db.Column(db.String(128), nullable=False)
    categoria = db.Column(db.String(32), nullable=False)
    geometria = db.Column(db.JSON, nullable=False)  # GeoJSON WGS84, longitude/latitude
    atributos = db.Column(db.JSON, nullable=False, default=dict)


class AssociacaoInfraestruturaTrecho(db.Model):
    """Resultado versionado da classificação espacial de um trecho GTFS."""

    __tablename__ = "associacoes_infraestrutura_trechos"
    __table_args__ = (
        db.ForeignKeyConstraint(["trecho_id"], ["trechos.id"],
                                name="fk_assoc_infra_trecho", ondelete="RESTRICT"),
        UniqueConstraint("trecho_id", "versao_conjunto", name="uq_assoc_infra_trecho_conjunto"),
        CheckConstraint(
            "classificacao IN ('CORREDOR', 'FAIXA_EXCLUSIVA', 'MISTO', "
            "'COBERTURA_PARCIAL', 'SEM_COBERTURA')",
            name="ck_assoc_infra_classificacao",
        ),
        CheckConstraint("fracao_corredor >= 0 AND fracao_corredor <= 1",
                        name="ck_assoc_infra_fracao_corredor"),
        CheckConstraint("fracao_faixa >= 0 AND fracao_faixa <= 1",
                        name="ck_assoc_infra_fracao_faixa"),
        CheckConstraint("fracao_sem_cobertura >= 0 AND fracao_sem_cobertura <= 1",
                        name="ck_assoc_infra_fracao_sem_cobertura"),
        Index("ix_assoc_infra_conjunto_classificacao", "versao_conjunto", "classificacao"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    trecho_id = db.Column(db.String(36), nullable=False)
    versao_conjunto = db.Column(db.String(64), nullable=False)
    versoes_infraestrutura = db.Column(db.JSON, nullable=False)
    classificacao = db.Column(db.String(32), nullable=False)
    fracao_corredor = db.Column(db.Float, nullable=False)
    fracao_faixa = db.Column(db.Float, nullable=False)
    fracao_sem_cobertura = db.Column(db.Float, nullable=False)
    categorias_confirmadas = db.Column(db.JSON, nullable=False, default=list)
    feicoes_associadas = db.Column(db.JSON, nullable=False, default=list)
    regra = db.Column(db.JSON, nullable=False)
    calculada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)


class VersaoTerritorial(db.Model):
    """Fonte versionada dos limites de distritos e subprefeituras."""

    __tablename__ = "versoes_territoriais"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_versao_territorial_sha256"),
        CheckConstraint("crs_origem <> ''", name="ck_versao_territorial_crs"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    fonte = db.Column(db.String(255), nullable=False)
    url_fonte = db.Column(db.String(1024), nullable=False)
    versao_fonte = db.Column(db.String(64), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    crs_origem = db.Column(db.String(32), nullable=False)
    capturada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    metadados = db.Column(db.JSON, nullable=False, default=dict)


class UnidadeTerritorial(db.Model):
    """Polígono de um distrito ou subprefeitura em GeoJSON EPSG:4326."""

    __tablename__ = "unidades_territoriais"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["subprefeitura_id", "versao_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_unidade_subprefeitura_versao", ondelete="RESTRICT",
        ),
        UniqueConstraint("versao_id", "tipo", "codigo", name="uq_unidade_territorial_codigo"),
        UniqueConstraint("id", "versao_id", name="uq_unidade_territorial_id_versao"),
        CheckConstraint("tipo IN ('DISTRITO', 'SUBPREFEITURA')", name="ck_unidade_territorial_tipo"),
        CheckConstraint(
            "(tipo = 'DISTRITO' AND subprefeitura_id IS NOT NULL) OR "
            "(tipo = 'SUBPREFEITURA' AND subprefeitura_id IS NULL)",
            name="ck_unidade_territorial_hierarquia",
        ),
        Index("ix_unidade_territorial_versao_tipo", "versao_id", "tipo"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    versao_id = db.Column(
        db.String(36), db.ForeignKey(
            "versoes_territoriais.id", ondelete="RESTRICT", name="fk_unidade_territorial_versao",
        ), nullable=False,
    )
    tipo = db.Column(db.String(16), nullable=False)
    codigo = db.Column(db.String(32), nullable=False)
    nome = db.Column(db.String(128), nullable=False)
    identificador_fonte = db.Column(db.String(32), nullable=False)
    subprefeitura_id = db.Column(db.String(36), nullable=True)
    geometria = db.Column(db.JSON, nullable=False)

    versao = db.relationship("VersaoTerritorial")
    subprefeitura = db.relationship(
        "UnidadeTerritorial", remote_side=[id, versao_id],
        foreign_keys=[subprefeitura_id, versao_id], overlaps="versao",
    )


class ParadaTerritorial(db.Model):
    """Atribuição única de parada a distrito/subprefeitura por versões."""

    __tablename__ = "paradas_territorios"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_codigo"],
            ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_parada_territorio_rede", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["distrito_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_parada_territorio_distrito", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["subprefeitura_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_parada_territorio_subprefeitura", ondelete="RESTRICT",
        ),
        Index("ix_parada_territorio_distrito", "versao_territorial_id", "distrito_id"),
        Index("ix_parada_territorio_subprefeitura", "versao_territorial_id", "subprefeitura_id"),
    )

    versao_gtfs_id = db.Column(db.String(36), primary_key=True)
    parada_codigo = db.Column(CodigoGTFS, primary_key=True)
    versao_territorial_id = db.Column(db.String(36), primary_key=True)
    distrito_id = db.Column(db.String(36), nullable=False)
    subprefeitura_id = db.Column(db.String(36), nullable=False)
    ponto_na_fronteira = db.Column(db.Boolean, nullable=False, default=False)
    qualidade = db.Column(db.JSON, nullable=False, default=dict)


class TrechoTerritorial(db.Model):
    """Território principal de cada trecho, escolhido pela maior sobreposição."""

    __tablename__ = "trechos_territorios"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["trecho_id", "versao_gtfs_id", "linha_codigo"],
            ["trechos.id", "trechos.versao_gtfs_id", "trechos.linha_codigo"],
            name="fk_trecho_territorio_rede", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["distrito_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_trecho_territorio_distrito", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["subprefeitura_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_trecho_territorio_subprefeitura", ondelete="RESTRICT",
        ),
        CheckConstraint("fracao_distrito > 0 AND fracao_distrito <= 1", name="ck_trecho_territorio_fracao_distrito"),
        CheckConstraint("fracao_subprefeitura > 0 AND fracao_subprefeitura <= 1", name="ck_trecho_territorio_fracao_subpref"),
        Index("ix_trecho_territorio_distrito", "versao_territorial_id", "distrito_id"),
        Index("ix_trecho_territorio_subprefeitura", "versao_territorial_id", "subprefeitura_id"),
    )

    trecho_id = db.Column(db.String(36), primary_key=True)
    versao_gtfs_id = db.Column(db.String(36), primary_key=True)
    linha_codigo = db.Column(CodigoGTFS, primary_key=True)
    versao_territorial_id = db.Column(db.String(36), primary_key=True)
    distrito_id = db.Column(db.String(36), nullable=False)
    subprefeitura_id = db.Column(db.String(36), nullable=False)
    fracao_distrito = db.Column(Double, nullable=False)
    fracao_subprefeitura = db.Column(Double, nullable=False)
    cruza_limite = db.Column(db.Boolean, nullable=False)
    qualidade = db.Column(db.JSON, nullable=False, default=dict)


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
    # US20-T02C: uma entrada por tentativa {tentativa, inicio, fim, estado, erro};
    # ``erro`` acima guarda só a última, o histórico preserva todas para auditoria.
    historico = db.Column(db.JSON, nullable=True)

    versao_gtfs = db.relationship("VersaoGTFS")
    resultados_reprocessamento = db.relationship(
        "ResultadoReprocessamento", back_populates="execucao_processamento"
    )


class PedidoExtracao(db.Model):
    """US28: pedido público de extração de brutos (diagrama de classes, p. 3/5).

    Um pedido tem exatamente um job (``ExecucaoProcessamento``), criado na mesma
    transação; o estado público deriva dele (``estado_publico``), sem coluna de
    estado própria. ``linha_codigo`` é o código Olho Vivo (``cl``), o mesmo de
    ``RegistroBrutoGPS.linha_origem``. ``arquivo`` é o resultado final, gravado
    pelo ExportService junto com o PRONTO do job.
    """

    __tablename__ = "pedidos_extracao"
    __table_args__ = (
        CheckConstraint("formato IN ('CSV', 'PARQUET')", name="ck_pedidos_extracao_formato"),
        CheckConstraint("fim_periodo > inicio_periodo", name="ck_pedidos_extracao_periodo"),
        UniqueConstraint("execucao_processamento_id", name="uq_pedidos_extracao_execucao"),
        {"mysql_engine": "InnoDB"},
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    linha_codigo = db.Column(db.String(16), nullable=False)
    inicio_periodo = db.Column(UTCDateTime, nullable=False)
    fim_periodo = db.Column(UTCDateTime, nullable=False)
    formato = db.Column(db.String(8), nullable=False)
    criado_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    versao_base = db.Column(db.String(128), nullable=False)
    execucao_processamento_id = db.Column(
        db.String(36),
        db.ForeignKey("execucoes_processamento.id", name="fk_pedidos_extracao_execucao",
                      ondelete="RESTRICT"),
        nullable=False,
    )
    arquivo_id = db.Column(
        db.String(36),
        db.ForeignKey("arquivos_armazenados.id", name="fk_pedidos_extracao_arquivo",
                      ondelete="RESTRICT"),
        nullable=True,
    )

    execucao = db.relationship("ExecucaoProcessamento")
    arquivo = db.relationship("ArquivoArmazenado")

    def estado_publico(self, limite_tentativas):
        """AGUARDANDO | PROCESSANDO | PRONTO | ERRO, derivado só do job.

        ERRO com tentativas restantes volta a ser reservado pelo worker, então
        publicamente ainda aguarda. PRONTO exige o arquivo publicado: um job
        PRONTO sem arquivo é inconsistência e não é anunciado como pronto.
        """
        job = self.execucao
        if job.estado == "ERRO":
            return "AGUARDANDO" if job.tentativas < limite_tentativas else "ERRO"
        if job.estado == "PRONTO" and self.arquivo_id is None:
            return "ERRO"
        return job.estado


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


class LimiteConsultaIP(db.Model):
    """Um contador por IP normalizado (hash), compartilhado pelos workers US11."""

    __tablename__ = "limites_consultas_ip"
    __table_args__ = (
        CheckConstraint("janela >= 0", name="ck_limites_ip_janela"),
        CheckConstraint("tentativas BETWEEN 1 AND 61", name="ck_limites_ip_tentativas"),
        Index("ix_limites_ip_janela", "janela"),
        {"mysql_engine": "InnoDB"},
    )
    chave_ip = db.Column(db.String(64), primary_key=True)
    janela = db.Column(db.BigInteger, nullable=False)
    tentativas = db.Column(db.Integer, nullable=False)


class ViagemObservada(db.Model):
    """Reconstrução versionada da viagem associada a uma partida programada.

    US14-T01C é responsável por produzir a observação. Este registro contém
    os fatos mínimos usados pelo cálculo de cumprimento, sem tocar no GPS bruto.
    """

    __tablename__ = "viagens_observadas"
    __table_args__ = (
        UniqueConstraint("partida_programada_id", "versao_reconstrucao",
                         name="uq_viagem_observada_partida_versao"),
        CheckConstraint("cobertura_tracado BETWEEN 0 AND 1",
                        name="ck_viagem_observada_cobertura"),
        CheckConstraint("fim_observado IS NULL OR fim_observado >= saida_observada",
                        name="ck_viagem_observada_intervalo"),
        Index("ix_viagens_observadas_reconstrucao", "versao_reconstrucao"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    partida_programada_id = db.Column(
        db.String(36), db.ForeignKey("partidas_programadas.id",
                                     name="fk_viagem_observada_partida", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    saida_observada = db.Column(UTCDateTime, nullable=False)
    fim_observado = db.Column(UTCDateTime, nullable=True)
    cobertura_tracado = db.Column(Double, nullable=False)
    versao_reconstrucao = db.Column(db.String(128), nullable=False)
    versao_correspondencia = db.Column(db.String(128), nullable=False)
    qualidade = db.Column(db.JSON, nullable=False, default=dict)

    partida_programada = db.relationship("PartidaProgramada")


class PassagemEstimada(db.Model):
    """Instante estimado em que uma viagem passou por uma parada (US15-T01).

    Diagrama: ViagemObservada 1 — 0..* PassagemEstimada 0..* — 1 Parada, com
    `instante` e `metodo`. Acréscimos para rastreabilidade: a versão da
    estimativa e as duas posições GPS brutas (imutáveis) que cercam a parada.
    Uma passagem por viagem, parada e versão da estimativa.
    """

    __tablename__ = "passagens_estimadas"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_codigo"], ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_passagens_estimadas_parada", ondelete="RESTRICT",
        ),
        UniqueConstraint("viagem_observada_id", "parada_codigo", "versao_estimativa",
                         name="uq_passagens_estimadas_viagem_parada_versao"),
        Index("ix_passagens_estimadas_parada_instante",
              "versao_gtfs_id", "parada_codigo", "instante"),
        {"mysql_engine": "InnoDB"},
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    viagem_observada_id = db.Column(
        db.String(36), db.ForeignKey("viagens_observadas.id",
                                     name="fk_passagens_estimadas_viagem", ondelete="RESTRICT"),
        nullable=False,
    )
    versao_gtfs_id = db.Column(db.String(36), nullable=False)
    parada_codigo = db.Column(CodigoGTFS, nullable=False)
    instante = db.Column(UTCDateTime, nullable=False)
    metodo = db.Column(db.String(64), nullable=False)
    versao_estimativa = db.Column(db.String(128), nullable=False)
    posicao_anterior_id = db.Column(
        IdGrande, db.ForeignKey("registros_gps.id", name="fk_passagens_estimadas_anterior",
                                ondelete="RESTRICT"), nullable=False,
    )
    posicao_posterior_id = db.Column(
        IdGrande, db.ForeignKey("registros_gps.id", name="fk_passagens_estimadas_posterior",
                                ondelete="RESTRICT"), nullable=False,
    )

    viagem = db.relationship("ViagemObservada")
    parada = db.relationship("Parada")
    posicao_anterior = db.relationship("RegistroBrutoGPS", foreign_keys=[posicao_anterior_id])
    posicao_posterior = db.relationship("RegistroBrutoGPS", foreign_keys=[posicao_posterior_id])


class EstimativaPassagemViagem(db.Model):
    """Execução da US15-T01 para uma viagem: detalhamento de ``PassagemEstimada``.

    Registra que a estimativa rodou (com qual lacuna máxima) e, para cada parada
    do percurso sem passagem, o intervalo em que ela poderia ter ocorrido. Sem
    este registro a US15-T02 não afirma zero observado.
    """

    __tablename__ = "estimativas_passagem_viagem"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    viagem_observada_id = db.Column(
        db.String(36), db.ForeignKey("viagens_observadas.id",
                                     name="fk_estimativas_passagem_viagem", ondelete="RESTRICT"),
        primary_key=True,
    )
    versao_estimativa = db.Column(db.String(128), primary_key=True)
    lacuna_maxima_seg = db.Column(Double, nullable=False)
    executada_em = db.Column(UTCDateTime, nullable=False)
    # [{"parada": código, "inicio": ISO|None, "fim": ISO|None}]
    sem_evidencia = db.Column(db.JSON, nullable=False, default=list)


class IndicadorOperacional(db.Model):
    """Valor publicado e versionado para a API pública (US11/US14).

    O conjunto é produzido pelo IndicatorService; a API só consulta valores
    publicados e não deriva métricas de posições GPS brutas.
    """

    __tablename__ = "indicadores_operacionais"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "linha_codigo"],
            ["linhas.versao_gtfs_id", "linhas.codigo"],
            name="fk_indicadores_linha_gtfs", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_codigo"],
            ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_indicadores_parada_gtfs", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["trecho_id", "versao_gtfs_id", "linha_codigo"],
            ["trechos.id", "trechos.versao_gtfs_id", "trechos.linha_codigo"],
            name="fk_indicadores_trecho_linha", ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "chave_resultado", name="uq_indicadores_chave_resultado",
        ),
        CheckConstraint("periodo_fim > periodo_inicio", name="ck_indicador_periodo"),
        CheckConstraint("parada_codigo IS NULL OR trecho_id IS NULL",
                        name="ck_indicador_parada_ou_trecho"),
        CheckConstraint("sentido IS NULL OR sentido IN ('0', '1')",
                        name="ck_indicador_sentido"),
        CheckConstraint("contagem_numerador IS NULL OR contagem_numerador >= 0",
                        name="ck_indicador_numerador"),
        CheckConstraint("contagem_denominador IS NULL OR contagem_denominador >= 0",
                        name="ck_indicador_denominador"),
        CheckConstraint("contagem_numerador IS NULL OR contagem_denominador IS NULL OR "
                        "contagem_numerador <= contagem_denominador",
                        name="ck_indicador_contagens"),
        CheckConstraint("contagem_denominador IS NULL OR contagem_denominador > 0 OR valor IS NULL",
                        name="ck_indicador_denominador_zero_sem_valor"),
        Index("ix_indicadores_linha_sentido_tipo_periodo", "linha_codigo", "sentido", "indicador", "periodo_inicio"),
        Index("ix_indicadores_parada_periodo", "parada_codigo", "periodo_inicio"),
        Index("ix_indicadores_trecho_periodo", "trecho_id", "periodo_inicio"),
        Index("ix_indicadores_versao", "versao_conjunto"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    chave_resultado = db.Column(db.String(64), nullable=False,
                                 default=lambda: uuid4().hex)
    versao_gtfs_id = db.Column(db.String(36), nullable=False)
    linha_codigo = db.Column(CodigoGTFS, nullable=False)
    sentido = db.Column(db.String(1), nullable=True)
    parada_codigo = db.Column(CodigoGTFS, nullable=True)
    trecho_id = db.Column(db.String(36), nullable=True)
    indicador = db.Column(db.String(64), nullable=False)
    valor = db.Column(Double, nullable=True)
    unidade = db.Column(db.String(32), nullable=False)
    periodo_inicio = db.Column(UTCDateTime, nullable=False)
    periodo_fim = db.Column(UTCDateTime, nullable=False)
    contagem_numerador = db.Column(db.BigInteger, nullable=True)
    contagem_denominador = db.Column(db.BigInteger, nullable=True)
    versao_conjunto = db.Column(db.String(128), nullable=False)
    versao_correspondencia = db.Column(db.String(128), nullable=True)
    versao_calculo = db.Column(db.String(128), nullable=True)
    qualidade = db.Column(db.JSON, nullable=False, default=dict)
    publicado_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)



# US05: documento novo catalogado em até 30 dias após a descoberta.
PRAZO_CATALOGACAO = timedelta(days=30)


class DocumentoOficial(db.Model):
    """PDF oficial (contrato, indicador) catalogado com metadados e procedência.

    O PDF original fica no FileStore e é referenciado por ArquivoArmazenado;
    um mesmo arquivo pode ser catalogado por mais de um documento (0..*).
    `descoberto_em` e `catalogado_em` permitem auditar o prazo de 30 dias.
    """

    __tablename__ = "documentos_oficiais"
    __table_args__ = (
        CheckConstraint("catalogado_em >= descoberto_em",
                        name="ck_documentos_oficiais_catalogacao_apos_descoberta"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    titulo = db.Column(db.String(500), nullable=False)
    tipo = db.Column(db.String(64), nullable=False)
    data_documento = db.Column(db.Date, nullable=False)
    orgao = db.Column(db.String(255), nullable=False)
    fonte = db.Column(db.String(1024), nullable=False)
    descoberto_em = db.Column(UTCDateTime, nullable=False)
    catalogado_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    arquivo_id = db.Column(
        db.String(36),
        db.ForeignKey("arquivos_armazenados.id", name="fk_documentos_oficiais_arquivo",
                      ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    arquivo = db.relationship("ArquivoArmazenado")

    @property
    def tempo_ate_catalogacao(self):
        return self.catalogado_em - self.descoberto_em

    @property
    def catalogado_no_prazo(self):
        return self.tempo_ate_catalogacao <= PRAZO_CATALOGACAO


# US06 — Clima por trecho. Detalha a parte CLIMA de `MedicaoContexto` do
# diagrama (página 4) em propostas novas da US06, sem atribuí-las ao modelo
# anterior: a estação (onde se mede), a observação (o que foi medido e quando)
# e o vínculo espacial trecho → estação usada. A medição por trecho e janela é
# derivada desses registros na consulta (docs/US06.md).
FONTES_CLIMA = ("INMET", "CGE")


class EstacaoMeteorologica(db.Model):
    """Estação ou posto de uma fonte de clima, com as coordenadas publicadas."""

    __tablename__ = "estacoes_meteorologicas"
    __table_args__ = (
        UniqueConstraint("fonte", "codigo", name="uq_estacoes_meteorologicas_fonte_codigo"),
        CheckConstraint("fonte IN ('INMET', 'CGE')", name="ck_estacoes_meteorologicas_fonte"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_estacoes_meteorologicas_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_estacoes_meteorologicas_longitude"),
        CheckConstraint("intervalo_seg > 0", name="ck_estacoes_meteorologicas_intervalo"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    fonte = db.Column(db.String(16), nullable=False)
    # Código na fonte (ex.: A701 no INMET).
    codigo = db.Column(db.String(32), nullable=False)
    nome = db.Column(db.String(255), nullable=False)
    # Graus decimais WGS84, como publicados (mesma precisão de `Parada`).
    latitude = db.Column(db.Numeric(11, 8), nullable=False)
    longitude = db.Column(db.Numeric(11, 8), nullable=False)
    altitude_m = db.Column(db.Numeric(8, 2), nullable=True)
    # Periodicidade nativa da fonte: 3600 s no INMET, 900 s no CGE.
    intervalo_seg = db.Column(db.Integer, nullable=False)
    operacao_inicio = db.Column(db.Date, nullable=True)
    situacao_fonte = db.Column(db.String(32), nullable=True)
    versao_fonte = db.Column(db.String(64), nullable=False)
    atualizada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)


class ColetaClima(db.Model):
    """Cada tentativa de coleta de uma fonte de clima, inclusive indisponível.

    Separada de `ExecucaoColeta`, que mede a cobertura dos ciclos GPS: misturar
    as duas faria a qualidade da coleta de ônibus contar chamadas de clima.
    """

    __tablename__ = "coletas_clima"
    __table_args__ = (
        CheckConstraint("fonte IN ('INMET', 'CGE')", name="ck_coletas_clima_fonte"),
        CheckConstraint(
            "estado IN ('EM_ANDAMENTO', 'OK', 'PARCIAL', 'FALHA', 'INDISPONIVEL')",
            name="ck_coletas_clima_estado",
        ),
        Index("ix_coletas_clima_fonte_iniciada", "fonte", "iniciada_em"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    fonte = db.Column(db.String(16), nullable=False)
    prevista_em = db.Column(UTCDateTime, nullable=False)
    iniciada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    finalizada_em = db.Column(UTCDateTime, nullable=True)
    estado = db.Column(db.String(16), nullable=False, default="EM_ANDAMENTO")
    periodo_inicio = db.Column(UTCDateTime, nullable=True)
    periodo_fim = db.Column(UTCDateTime, nullable=True)
    recebidas = db.Column(db.Integer, nullable=False, default=0)
    novas = db.Column(db.Integer, nullable=False, default=0)
    # Só o tipo do erro ou um motivo curto: URLs da fonte podem conter o token.
    erro = db.Column(db.String(255), nullable=True)


class ObservacaoClima(db.Model):
    """Uma medição de uma estação num instante, com unidade explícita.

    `instante` é o fim do intervalo de acumulação (UTC). `chuva` é o total
    acumulado em (instante - intervalo_seg, instante]; `temperatura` é a
    leitura instantânea em `instante`. NULL = a fonte não informou e nunca
    significa ausência de chuva; zero só existe quando a fonte publica 0.
    """

    __tablename__ = "observacoes_clima"
    __table_args__ = (
        UniqueConstraint("estacao_id", "instante", name="uq_observacoes_clima_estacao_instante"),
        CheckConstraint("chuva IS NULL OR chuva >= 0", name="ck_observacoes_clima_chuva"),
        CheckConstraint("intervalo_seg > 0", name="ck_observacoes_clima_intervalo"),
        CheckConstraint("unidade_chuva = 'mm'", name="ck_observacoes_clima_unidade_chuva"),
        CheckConstraint("unidade_temperatura = 'degC'",
                        name="ck_observacoes_clima_unidade_temperatura"),
        Index("ix_observacoes_clima_instante", "instante"),
    )

    id = db.Column(IdGrande, primary_key=True, autoincrement=True)
    estacao_id = db.Column(
        db.String(36), db.ForeignKey("estacoes_meteorologicas.id", ondelete="RESTRICT",
                                     name="fk_observacoes_clima_estacao"),
        nullable=False,
    )
    instante = db.Column(UTCDateTime, nullable=False)
    intervalo_seg = db.Column(db.Integer, nullable=False)
    chuva = db.Column(db.Numeric(7, 2), nullable=True)
    unidade_chuva = db.Column(db.String(8), nullable=False, default="mm")
    temperatura = db.Column(db.Numeric(5, 2), nullable=True)
    unidade_temperatura = db.Column(db.String(8), nullable=False, default="degC")
    fonte = db.Column(db.String(16), nullable=False)
    versao_fonte = db.Column(db.String(64), nullable=False)
    coletada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
    coleta_id = db.Column(
        db.String(36), db.ForeignKey("coletas_clima.id", ondelete="RESTRICT",
                                     name="fk_observacoes_clima_coleta"),
        nullable=True,
    )

    estacao = db.relationship("EstacaoMeteorologica")


class TrechoEstacaoClima(db.Model):
    """Vínculo espacial: a estação de uma fonte usada para um trecho.

    Distância mínima entre o traçado do trecho e a estação, em metros, medida
    em SIRGAS 2000 / UTM 23S. Sem estação dentro do raio, o vínculo continua
    registrado com `situacao` de bloqueio, para que nenhum trecho fique sem
    resposta explícita.
    """

    __tablename__ = "trechos_estacoes_clima"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["trecho_id", "versao_gtfs_id", "linha_codigo"],
            ["trechos.id", "trechos.versao_gtfs_id", "trechos.linha_codigo"],
            name="fk_trecho_estacao_clima_rede", ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["estacao_id"], ["estacoes_meteorologicas.id"],
            name="fk_trecho_estacao_clima_estacao", ondelete="RESTRICT",
        ),
        CheckConstraint("fonte IN ('INMET', 'CGE')", name="ck_trecho_estacao_clima_fonte"),
        CheckConstraint(
            "situacao IN ('ASSOCIADO', 'FORA_DO_RAIO', 'SEM_ESTACAO', 'GEOMETRIA_INVALIDA')",
            name="ck_trecho_estacao_clima_situacao",
        ),
        # Sem estação ou sem traçado válido não há distância; com estação, sempre há.
        CheckConstraint(
            "(situacao IN ('SEM_ESTACAO', 'GEOMETRIA_INVALIDA') AND estacao_id IS NULL "
            "AND distancia_m IS NULL) OR "
            "(situacao IN ('ASSOCIADO', 'FORA_DO_RAIO') AND estacao_id IS NOT NULL AND distancia_m >= 0)",
            name="ck_trecho_estacao_clima_coerencia",
        ),
        Index("ix_trecho_estacao_clima_estacao", "estacao_id"),
        Index("ix_trecho_estacao_clima_fonte_versao", "fonte", "versao_gtfs_id"),
    )

    trecho_id = db.Column(db.String(36), primary_key=True)
    fonte = db.Column(db.String(16), primary_key=True)
    versao_gtfs_id = db.Column(db.String(36), nullable=False)
    linha_codigo = db.Column(CodigoGTFS, nullable=False)
    estacao_id = db.Column(db.String(36), nullable=True)
    distancia_m = db.Column(Double, nullable=True)
    raio_maximo_m = db.Column(Double, nullable=False)
    situacao = db.Column(db.String(24), nullable=False)
    crs_medicao = db.Column(db.String(16), nullable=False)
    versao_regra = db.Column(db.String(32), nullable=False)
    calculado_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)

    estacao = db.relationship("EstacaoMeteorologica")


TIPOS_CONTEXTO = ("CLIMA", "INFRAESTRUTURA", "TRAFEGO")


class MedicaoContexto(db.Model):
    """Medição de contexto associada a trecho e período (diagrama, página 4).

    Hoje só a US08 (tipo TRAFEGO) grava aqui; clima e infraestrutura têm
    registros próprios (US06/US07). ``dados`` segue o esquema do tipo e
    ``unidades`` dá a unidade de cada campo numérico. Acréscimo ao diagrama:
    ``chave``, a identidade determinística do resultado (reprocessar a mesma
    janela atualiza o mesmo registro, sem duplicar).
    """

    __tablename__ = "medicoes_contexto"
    __table_args__ = (
        UniqueConstraint("chave", name="uq_medicoes_contexto_chave"),
        CheckConstraint("tipo IN ('CLIMA', 'INFRAESTRUTURA', 'TRAFEGO')",
                        name="ck_medicoes_contexto_tipo"),
        CheckConstraint("fim IS NULL OR fim > inicio", name="ck_medicoes_contexto_periodo"),
        Index("ix_medicoes_contexto_trecho_tipo_inicio", "trecho_id", "tipo", "inicio"),
        {"mysql_engine": "InnoDB"},
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    chave = db.Column(db.String(64), nullable=False)
    tipo = db.Column(db.String(16), nullable=False)
    trecho_id = db.Column(db.String(36), db.ForeignKey(
        "trechos.id", name="fk_medicoes_contexto_trecho", ondelete="RESTRICT"), nullable=False)
    inicio = db.Column(UTCDateTime, nullable=False)
    fim = db.Column(UTCDateTime, nullable=True)
    fonte = db.Column(db.String(64), nullable=False)
    versao_fonte = db.Column(db.String(128), nullable=False)
    dados = db.Column(db.JSON, nullable=False)
    unidades = db.Column(db.JSON, nullable=False)
    coletada_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)

    trecho = db.relationship("Trecho")


class VerificacaoSaude(db.Model):
    """US30: uma sondagem externa de um componente (diagrama de classes, p. 4/5).

    Gravada pelo monitor (``scripts/sondagem.py``), processo separado do Flask.
    Independente da coleta: a taxa de sucesso dos ciclos vem de ``ExecucaoColeta``.
    ``estado`` usa o vocabulário do diagrama; ``DEGRADADO`` está reservado até o
    PO definir um limiar de latência (resposta lenta dentro do timeout é ``OK``
    com ``latencia_ms`` registrada; acima do timeout é ``INDISPONIVEL``).
    """

    __tablename__ = "verificacoes_saude"
    __table_args__ = (
        CheckConstraint("componente IN ('API', 'PAINEL')", name="ck_verificacao_saude_componente"),
        CheckConstraint("estado IN ('OK', 'DEGRADADO', 'INDISPONIVEL')",
                        name="ck_verificacao_saude_estado"),
        CheckConstraint("latencia_ms >= 0", name="ck_verificacao_saude_latencia"),
        CheckConstraint("status_http IS NULL OR (status_http >= 100 AND status_http <= 599)",
                        name="ck_verificacao_saude_http"),
        Index("ix_verificacao_saude_componente_instante", "componente", "verificada_em"),
        {"mysql_engine": "InnoDB"},
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    componente = db.Column(db.String(16), nullable=False)
    verificada_em = db.Column(UTCDateTime, nullable=False)
    estado = db.Column(db.String(16), nullable=False)
    latencia_ms = db.Column(db.Integer, nullable=False)
    status_http = db.Column(db.Integer, nullable=True)
    # OK | HTTP_ERRO | TIMEOUT | ERRO_CONEXAO | CONTEUDO_INESPERADO (sem URL nem mensagem bruta).
    detalhe = db.Column(db.String(32), nullable=False)
    # Rótulo do que a sonda comprova (ex.: "GET /saude"), sem host, credencial ou query.
    alvo = db.Column(db.String(64), nullable=False)


class RelatorioAssociacaoMensal(db.Model):
    """Snapshot mensal revisado ou estado de análise inválida (US21)."""
    __tablename__ = 'relatorios_associacao_mensal'
    __table_args__ = (
        CheckConstraint("situacao IN ('PUBLICADO', 'INVALIDO')", name='ck_relatorio_associacao_situacao'),
        {'mysql_engine': 'InnoDB'},
    )
    mes = db.Column(db.String(7), primary_key=True)
    situacao = db.Column(db.String(16), nullable=False)
    dados = db.Column(db.JSON, nullable=False)
    hash_dados = db.Column(db.String(64), nullable=False)
    registrado_em = db.Column(UTCDateTime, nullable=False, default=agora_utc)
