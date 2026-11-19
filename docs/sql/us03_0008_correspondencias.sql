-- Running upgrade 0007 -> us03_0008

CREATE TABLE tabelas_correspondencia (
    id VARCHAR(36) NOT NULL,
    versao VARCHAR(96) NOT NULL,
    versao_gtfs_id VARCHAR(36) NOT NULL,
    vigencia_inicio DATE NOT NULL,
    vigencia_fim DATE,
    fonte VARCHAR(255) NOT NULL,
    hash VARCHAR(64) NOT NULL,
    criada_em DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_tabela_correspondencia_gtfs FOREIGN KEY(versao_gtfs_id) REFERENCES versoes_gtfs (id) ON DELETE RESTRICT,
    CONSTRAINT uq_tabela_correspondencia_gtfs UNIQUE (id, versao_gtfs_id),
    CONSTRAINT ck_tabela_correspondencia_vigencia CHECK (vigencia_fim IS NULL OR vigencia_fim >= vigencia_inicio),
    UNIQUE (versao)
);

CREATE TABLE correspondencias_codigos (
    id VARCHAR(36) NOT NULL,
    tabela_id VARCHAR(36) NOT NULL,
    versao_gtfs_id VARCHAR(36) NOT NULL,
    codigo_olho_vivo VARCHAR(32) NOT NULL,
    sentido_olho_vivo VARCHAR(8) NOT NULL,
    linha_codigo VARCHAR(64) COLLATE utf8mb4_0900_bin,
    sentido_gtfs VARCHAR(8),
    motivo VARCHAR(255),
    PRIMARY KEY (id),
    CONSTRAINT uq_correspondencia_id_gtfs UNIQUE (id, versao_gtfs_id),
    CONSTRAINT fk_correspondencia_tabela_gtfs FOREIGN KEY(tabela_id, versao_gtfs_id) REFERENCES tabelas_correspondencia (id, versao_gtfs_id) ON DELETE RESTRICT,
    CONSTRAINT fk_correspondencia_linha FOREIGN KEY(versao_gtfs_id, linha_codigo) REFERENCES linhas (versao_gtfs_id, codigo) ON DELETE RESTRICT,
    CONSTRAINT ck_correspondencia_sentido_ov CHECK (sentido_olho_vivo IN ('1','2')),
    CONSTRAINT ck_correspondencia_sentido_gtfs CHECK (sentido_gtfs IS NULL OR sentido_gtfs IN ('0','1')),
    CONSTRAINT ck_correspondencia_destino CHECK ((linha_codigo IS NULL AND sentido_gtfs IS NULL) OR (linha_codigo IS NOT NULL AND sentido_gtfs IS NOT NULL))
);

CREATE INDEX ix_correspondencia_origem ON correspondencias_codigos (codigo_olho_vivo, sentido_olho_vivo);

ALTER TABLE observacoes_tratadas MODIFY valida BOOL NULL;

ALTER TABLE observacoes_tratadas ADD COLUMN linha_codigo VARCHAR(64) COLLATE utf8mb4_0900_bin;

ALTER TABLE observacoes_tratadas ADD COLUMN sentido_gtfs VARCHAR(8);

ALTER TABLE observacoes_tratadas ADD COLUMN correspondencia_id VARCHAR(36);

ALTER TABLE observacoes_tratadas ADD COLUMN motivo_correspondencia VARCHAR(64);

ALTER TABLE observacoes_tratadas ADD COLUMN candidatos_correspondencia JSON;

ALTER TABLE observacoes_tratadas ADD COLUMN distancia_tracado_m FLOAT;

ALTER TABLE observacoes_tratadas ADD COLUMN versao_validacao VARCHAR(128);

ALTER TABLE observacoes_tratadas ADD COLUMN processada_em DATETIME(6);

ALTER TABLE observacoes_tratadas ADD CONSTRAINT fk_observacoes_linha_versionada FOREIGN KEY(versao_gtfs_id, linha_codigo) REFERENCES linhas (versao_gtfs_id, codigo) ON DELETE RESTRICT;

ALTER TABLE observacoes_tratadas ADD CONSTRAINT fk_observacoes_correspondencia FOREIGN KEY(correspondencia_id, versao_gtfs_id) REFERENCES correspondencias_codigos (id, versao_gtfs_id) ON DELETE RESTRICT;

UPDATE alembic_version SET version_num='us03_0008' WHERE alembic_version.version_num = '0007';
