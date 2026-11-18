-- US12-T01A: executar com usuário autorizado a criar bases no servidor escolhido.
-- Não apaga tabelas/dados nem altera bases que já existam.
SELECT VERSION() AS mysql_server_version,
       @@hostname AS servidor, @@port AS porta,
       @@default_storage_engine AS engine_padrao;

CREATE DATABASE IF NOT EXISTS sptrans_dev
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS sptrans_test
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- CREATE DATABASE não define engine; InnoDB é definido nas tabelas/sessão.
-- A consulta abaixo deve mostrar utf8mb4 nas DUAS bases.
-- Se já existiam com outro charset, revisar antes de alterar.
SELECT SCHEMA_NAME, DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME
FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME IN ('sptrans_dev', 'sptrans_test');

SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE
FROM information_schema.TABLES
WHERE TABLE_SCHEMA IN ('sptrans_dev', 'sptrans_test')
  AND TABLE_TYPE = 'BASE TABLE';
