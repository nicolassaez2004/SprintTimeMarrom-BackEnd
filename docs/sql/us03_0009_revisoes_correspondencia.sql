-- Running upgrade us03_0008 -> us03_0009

ALTER TABLE tabelas_correspondencia ADD COLUMN substitui_id VARCHAR(36);

ALTER TABLE tabelas_correspondencia ADD CONSTRAINT fk_tabela_correspondencia_anterior FOREIGN KEY(substitui_id, versao_gtfs_id) REFERENCES tabelas_correspondencia (id, versao_gtfs_id) ON DELETE RESTRICT;

ALTER TABLE tabelas_correspondencia ADD CONSTRAINT ck_tabela_correspondencia_nao_auto CHECK (substitui_id IS NULL OR substitui_id <> id);

UPDATE alembic_version SET version_num='us03_0009' WHERE alembic_version.version_num = 'us03_0008';
