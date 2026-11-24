-- SQL offline (flask db upgrade us29_0012:us11_0016 --sql), somente para revisão.
-- Aplicar com Flask-Migrate após o backup; ver docs/AIVEN-MIGRACOES-MVP.md.

-- Running upgrade us29_0012 -> us09_0013

CREATE INDEX ix_registros_gps_coletado_em_id ON registros_gps (coletado_em, id);

UPDATE alembic_version SET version_num='us09_0013' WHERE alembic_version.version_num = 'us29_0012';

-- Running upgrade us09_0013 -> us05_0014

CREATE TABLE documentos_oficiais (
    id VARCHAR(36) NOT NULL,
    titulo VARCHAR(500) NOT NULL,
    tipo VARCHAR(64) NOT NULL,
    data_documento DATE NOT NULL,
    orgao VARCHAR(255) NOT NULL,
    fonte VARCHAR(1024) NOT NULL,
    descoberto_em DATETIME(6) NOT NULL,
    catalogado_em DATETIME(6) NOT NULL,
    arquivo_id VARCHAR(36) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_documentos_oficiais_arquivo FOREIGN KEY(arquivo_id) REFERENCES arquivos_armazenados (id) ON DELETE RESTRICT,
    CONSTRAINT ck_documentos_oficiais_catalogacao_apos_descoberta CHECK (catalogado_em >= descoberto_em)
)ENGINE=InnoDB;

CREATE INDEX ix_documentos_oficiais_arquivo_id ON documentos_oficiais (arquivo_id);

UPDATE alembic_version SET version_num='us05_0014' WHERE alembic_version.version_num = 'us09_0013';

-- Running upgrade us05_0014 -> us11_0015

CREATE TABLE indicadores_operacionais (
    id VARCHAR(36) NOT NULL,
    linha_codigo VARCHAR(64) NOT NULL,
    indicador VARCHAR(64) NOT NULL,
    valor DOUBLE NOT NULL,
    unidade VARCHAR(32) NOT NULL,
    periodo_inicio DATETIME(6) NOT NULL,
    periodo_fim DATETIME(6) NOT NULL,
    versao_conjunto VARCHAR(128) NOT NULL,
    qualidade JSON NOT NULL,
    publicado_em DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_indicador_publicado_periodo_versao UNIQUE (linha_codigo, indicador, periodo_inicio, periodo_fim, versao_conjunto),
    CONSTRAINT ck_indicador_periodo CHECK (periodo_fim > periodo_inicio)
)ENGINE=InnoDB;

CREATE INDEX ix_indicadores_linha_indicador_inicio ON indicadores_operacionais (linha_codigo, indicador, periodo_inicio);

CREATE INDEX ix_indicadores_versao ON indicadores_operacionais (versao_conjunto);

UPDATE alembic_version SET version_num='us11_0015' WHERE alembic_version.version_num = 'us05_0014';

-- Running upgrade us11_0015 -> us11_0016

CREATE TABLE limites_consultas_ip (
    chave_ip VARCHAR(64) NOT NULL,
    janela BIGINT NOT NULL,
    tentativas INTEGER NOT NULL,
    PRIMARY KEY (chave_ip),
    CONSTRAINT ck_limites_ip_janela CHECK (janela >= 0),
    CONSTRAINT ck_limites_ip_tentativas CHECK (tentativas BETWEEN 1 AND 61)
)ENGINE=InnoDB;

CREATE INDEX ix_limites_ip_janela ON limites_consultas_ip (janela);

UPDATE alembic_version SET version_num='us11_0016' WHERE alembic_version.version_num = 'us11_0015';

