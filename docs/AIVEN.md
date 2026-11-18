# Conexão compartilhada na Aiven

Configurado e verificado em 18/11/2026 após o serviço entrar em Running. O `.env` deste computador agora aponta à Aiven. As bases remotas `sptrans_dev` e `sptrans_test` foram criadas com usuários próprios e TLS obrigatório. A configuração MySQL local anterior foi preservada fora do Git em `%LOCALAPPDATA%/TimeMarrom/aiven/env-local-anterior.backup`.

## Dados para configurar o Workbench

Criar duas conexões novas, preservando as conexões locais anteriores:

| Campo | Desenvolvimento | Teste |
| --- | --- | --- |
| Connection Name | Aiven Marrom - desenvolvimento | Aiven Marrom - teste |
| Connection Method | Standard TCP/IP | Standard TCP/IP |
| Hostname | sprintmarrom-projeto2progeficazmatheushermida.g.aivencloud.com | mesmo host |
| Port | 11500 | 11500 |
| Username | marrom_dev | marrom_test |
| Default Schema | sptrans_dev | sptrans_test |
| SSL / Use SSL | Require and Verify Identity | Require and Verify Identity |
| SSL CA File | C:/Users/mathe/AppData/Local/TimeMarrom/aiven/ca.pem | mesmo certificado |

Senhas específicas dessas conexões estão no arquivo local `%LOCALAPPDATA%/TimeMarrom/aiven/credenciais-equipe.json`. Não são as senhas do MySQL local nem a senha de avnadmin. Copiar somente o valor correspondente, sem aspas. Não colocar esse arquivo no Git/Trello. Clicar Test Connection nas duas; falta comprovar essa etapa visualmente.

Cada colega usa o host/porta e a base acima, mas precisa preencher seu próprio `.env` com as credenciais recebidas em canal privado e um caminho local válido para o certificado CA. Compartilhar o código por commit/push; nunca publicar o `.env`.

Resultado: verificador Flask abriu ambas as bases em MySQL Server 8.4.11, InnoDB, utf8mb4, com certificado/hostname verificados e teste de escrita/leitura. A tabela temporária de verificação passou a ter chave primária, exigida pela Aiven. Migrações de tabelas de negócio não foram executadas.

## Configuração TLS

A aplicação aceita `MYSQL_SSL_CA` com o caminho local do certificado CA baixado na Aiven. Para `mysql+pymysql`, ativa verificação da CA e do hostname; não desativar essas verificações para contornar erro de certificado. Essa opção não é passada ao SQLite dos testes.

Exemplo de `.env` (substituir os placeholders localmente):

```dotenv
DATABASE_URL=mysql+pymysql://USUARIO_DEV:SENHA_CODIFICADA@HOST:PORTA/sptrans_dev?charset=utf8mb4
TEST_DATABASE_URL=mysql+pymysql://USUARIO_TEST:SENHA_CODIFICADA@HOST:PORTA/sptrans_test?charset=utf8mb4
MYSQL_SSL_CA="C:/CAMINHO/ca.pem"
```

No Windows, usar barras `/` no caminho do `.env`. Cada colega informa o seu próprio caminho. Certificado CA é público; senhas e `.env` não devem entrar no Git. Na Aiven, a URI `mysql://...?...ssl-mode=REQUIRED` não deve ser colada diretamente no SQLAlchemy: usar `mysql+pymysql`, charset e a configuração CA acima.

## Etapas após o serviço ficar disponível

1. Conectar com o administrador usando CA e hostname verificados.
2. Criar `sptrans_dev` e `sptrans_test` com utf8mb4, preservando `defaultdb`.
3. Provisionar usuários separados, limitados à respectiva base e com TLS obrigatório.
4. Preencher `.env` local com esses usuários; guardar cópia da configuração local anterior fora do Git.
5. Executar `python -m scripts.verificar_bases_mysql` e confirmar negociação TLS.
6. Criar conexões novas no Workbench para a Aiven, preservando as conexões locais. Usar o host/porta da Aiven e **SSL > Require and Verify Identity**, com CA Certificate File apontando ao certificado local.
7. Aplicar migrações é etapa posterior: criar as bases não cria as tabelas de negócio automaticamente.

Uma senha administrativa foi compartilhada no chat; redefini-la no painel da Aiven antes de compartilhar acesso com a equipe. O backend do time deve usar os usuários específicos das bases.

Referências: [Aiven com Python](https://aiven.io/docs/products/mysql/howto/connect-with-python) e [parâmetros TLS PyMySQL](https://pymysql.readthedocs.io/en/latest/modules/connections.html).
