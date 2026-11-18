-- Running upgrade 0003 -> 0004

CREATE TABLE observacoes_tratadas (
    id VARCHAR(36) NOT NULL, 
    bruto_id BIGINT NOT NULL, 
    valida BOOL NOT NULL, 
    motivo_invalidez TEXT, 
    versao_correspondencia VARCHAR(128) NOT NULL, 
    versao_gtfs_id VARCHAR(36), 
    PRIMARY KEY (id), 
    CONSTRAINT fk_observacoes_tratadas_bruto FOREIGN KEY(bruto_id) REFERENCES registros_gps (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_observacoes_tratadas_gtfs FOREIGN KEY(versao_gtfs_id) REFERENCES versoes_gtfs (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_observacoes_tratadas_bruto UNIQUE (bruto_id), 
    CONSTRAINT ck_observacoes_tratadas_versao CHECK (LENGTH(TRIM(versao_correspondencia)) > 0)
)ENGINE=InnoDB;

CREATE INDEX ix_observacoes_tratadas_versao_gtfs_id ON observacoes_tratadas (versao_gtfs_id);

UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003';

