-- Running upgrade 0011_merge_us12_us03 -> us29_0012

CREATE TABLE lacunas_coleta (
    id VARCHAR(36) NOT NULL,
    inicio DATETIME(6) NOT NULL,
    fim DATETIME(6),
    causa VARCHAR(128) NOT NULL,
    fonte VARCHAR(32) NOT NULL,
    aberta_fonte VARCHAR(32),
    PRIMARY KEY (id),
    CONSTRAINT ck_lacuna_intervalo CHECK (fim IS NULL OR fim >= inicio),
    UNIQUE (aberta_fonte)
);

CREATE TABLE estados_coleta (
    fonte VARCHAR(32) NOT NULL,
    token VARCHAR(36) NOT NULL,
    PRIMARY KEY (fonte)
);

CREATE TABLE notificacoes_coleta (
    id VARCHAR(36) NOT NULL,
    lacuna_id VARCHAR(36),
    tipo VARCHAR(16) NOT NULL,
    criada_em DATETIME(6) NOT NULL,
    enviada_em DATETIME(6),
    canal VARCHAR(32) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_notificacao_lacuna FOREIGN KEY(lacuna_id) REFERENCES lacunas_coleta (id) ON DELETE RESTRICT,
    CONSTRAINT uq_notificacao_lacuna_tipo UNIQUE (lacuna_id, tipo)
);

ALTER TABLE execucoes_coleta ADD COLUMN chave_logica VARCHAR(36);

ALTER TABLE execucoes_coleta ADD COLUMN lacuna_id VARCHAR(36);

ALTER TABLE execucoes_coleta ADD COLUMN confirmada_em DATETIME(6);

ALTER TABLE execucoes_coleta ADD COLUMN duracao_gravacao_ms BIGINT;

ALTER TABLE execucoes_coleta ADD CONSTRAINT uq_execucoes_coleta_chave_logica UNIQUE (chave_logica);

ALTER TABLE execucoes_coleta ADD CONSTRAINT fk_execucao_coleta_lacuna FOREIGN KEY(lacuna_id) REFERENCES lacunas_coleta (id) ON DELETE RESTRICT;

UPDATE alembic_version SET version_num='us29_0012' WHERE alembic_version.version_num = '0011_merge_us12_us03';
