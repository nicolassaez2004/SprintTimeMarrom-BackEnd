# Aiven: aplicar `us09_0013` → `us11_0016` (para o responsável pelo banco)

**Situação em 24/11/2026:** uma consulta somente leitura mostrou
`sptrans_dev` e `sptrans_test` na revisão `us29_0012`. O head da `main` é
`us11_0016`. Sem as 4 migrações pendentes, `/api/documentos` (Acervo, US05)
e `/indicadores` (US11) respondem **503**. O mapa, a qualidade e os downloads
já funcionam em `us29_0012`, o que foi testado localmente.

Ninguém além do responsável autorizado executa estes passos.

## O que muda

| Revisão | Operação | Risco |
|---|---|---|
| `us09_0013` | `CREATE INDEX ix_registros_gps_coletado_em_id ON registros_gps (coletado_em, id)` | Índice na maior tabela (≈ 650 mil linhas em 23/11). No MySQL 8 é *online* (INPLACE), e o coletor pode continuar gravando. |
| `us05_0014` | Cria `documentos_oficiais` (FK para `arquivos_armazenados`) | Aditiva. O downgrade é **recusado** pela migração. |
| `us11_0015` | Cria `indicadores_operacionais` | Aditiva |
| `us11_0016` | Cria `limites_consultas_ip` | Aditiva. O downgrade é **recusado** pela migração. |

Nenhuma tabela ou coluna existente é alterada, e nenhum dado é apagado. O SQL
exato para revisão está em [`sql/aiven_us29_0012_a_us11_0016.sql`](sql/aiven_us29_0012_a_us11_0016.sql)
(gerado com `flask db upgrade us29_0012:us11_0016 --sql`).

## Ensaio já realizado (base local descartável, 24/11/2026)

1. Base em `us29_0012` com 390.373 brutos reais.
2. `flask db upgrade` aplicado até o head, e `flask db check` sem diferenças.
3. `flask db downgrade us29_0012` recusado, com o banco permanecendo consistente no head.
4. O rollback manual abaixo voltou o banco a `us29_0012`, com os brutos intactos.
5. O upgrade foi reaplicado (0,8 s) e terminou consistente.

Foi feito em MariaDB 13. Antes da Aiven, convém repetir num MySQL 8 local
(é o destino dos ensaios da US32).

## Procedimento na Aiven

1. **Conferir a revisão**, só com leitura:
   `SELECT version_num FROM alembic_version;` deve retornar `us29_0012`.
   Se retornar outro valor, **pare**.
2. **Fazer o backup (US32)**, para um destino fora do repositório e de
   preferência fora da máquina coletora:
   `python -m scripts.backup --destino <DIR_BACKUP>`. Guarde o diretório `backup-...` gerado.
3. *(Recomendado)* **Ensaiar a restauração** desse backup num MySQL 8 local:
   `RESTORE_DATABASE_URL=...marrom_restore_<id>` e depois
   `python -m scripts.restaurar --backup <backup-...> --destino-arquivos <DIR_NOVO>`.
4. **Aplicar**, com o `DATABASE_URL` da base alvo, a partir do código da
   `main` atualizada: `flask --app app db upgrade`. Repita para `sptrans_test`, se desejado.
5. **Validar:**
   - `flask --app app db current` → `us11_0016 (head)`
   - `flask --app app db check` → `No new upgrade operations detected.`
   - Com a API apontando para a base: `GET /api/documentos` → 200,
     `GET /indicadores` → 200 e `GET /posicoes/atuais` → 200.
   - `SELECT COUNT(*) FROM registros_gps;` deve ser maior ou igual à contagem anterior.

## Rollback

**Antes de qualquer uso**, isto é, enquanto `documentos_oficiais` e
`indicadores_operacionais` estiverem vazias, use o SQL ensaiado:

```sql
SELECT (SELECT COUNT(*) FROM documentos_oficiais) AS documentos,
       (SELECT COUNT(*) FROM indicadores_operacionais) AS indicadores; -- ambos 0
DROP TABLE limites_consultas_ip;      -- só contadores de requisição, descartáveis
DROP TABLE indicadores_operacionais;
DROP TABLE documentos_oficiais;
DROP INDEX ix_registros_gps_coletado_em_id ON registros_gps;
UPDATE alembic_version SET version_num = 'us29_0012' WHERE version_num = 'us11_0016';
```

**Se já houver documentos ou indicadores**, não use `DROP`. Mantenha o
esquema: ele é aditivo, e o código anterior simplesmente não usa as tabelas
novas. Ou restaure o backup do passo 2 seguindo `docs/US32.md`.

**Falha no meio do upgrade:** o MySQL não tem DDL transacional, então uma
revisão pode ficar parcialmente aplicada. Confira `alembic_version` e as
tabelas existentes, remova apenas o objeto criado pela revisão que falhou e
reexecute `flask db upgrade`.
