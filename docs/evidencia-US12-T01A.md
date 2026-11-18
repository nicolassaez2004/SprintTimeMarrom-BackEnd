# Evidência técnica — US12-T01A

## Atualização — Aiven compartilhada

Em 18/11/2026, após Running, foram criadas as bases remotas `sptrans_dev` e `sptrans_test`, com usuários separados limitados à própria base e TLS obrigatório. O `.env` ativo foi atualizado; a configuração local anterior foi preservada fora do Git. Verificador via Flask: ambas OK em MySQL 8.4.11, InnoDB e utf8mb4, com escrita/leitura temporária e verificação de certificado CA/hostname. Credenciais não publicadas no repositório. Conexões Workbench remotas ainda precisam ser testadas visualmente conforme AIVEN.md; os testes Workbench anteriores comprovam apenas o servidor local.

O histórico local abaixo é preservado para referência.

Executada em 18/11/2026 neste computador, no backend SprintTimeMarrom-BackEnd.

## Instalação

MySQL Community Server 8.0.46 Win64, arquivo oficial `mysql-8.0.46-winx64.zip` obtido de https://cdn.mysql.com/Downloads/MySQL-8.0/mysql-8.0.46-winx64.zip . MD5 conferido com a página oficial: `003f527d5df61b663ff191038cd676bd`.

Instalação no perfil do usuário, em `%LOCALAPPDATA%\TimeMarrom\mysql`. Processo local, sem serviço Windows nem inicialização automática. Listener verificado em `127.0.0.1:3306`. MySQL X desativado. Usuários `marrom_dev` e `marrom_test` com permissões apenas na respectiva base. Credenciais criadas aleatoriamente, salvas somente nos arquivos locais, sem publicação no Git.

## Resultado real do verificador

Comando: `.\.venv\Scripts\python.exe -m scripts.verificar_bases_mysql`

```text
DESENVOLVIMENTO: OK | base=sptrans_dev | MySQL Server=8.0.46 | InnoDB | utf8mb4
TESTE: OK | base=sptrans_test | MySQL Server=8.0.46 | InnoDB | utf8mb4
Duas bases verificadas pela aplicação. Anexar também evidência do Workbench.
```

Verifica abertura de conexão usando a aplicação Flask, identificação das bases, padrão InnoDB, charset e escrita/leitura de acentos e emoji em tabela temporária InnoDB. Servidor encerrado via SHUTDOWN e iniciado novamente com `iniciar-mysql.ps1`; o mesmo verificador passou após reinício. Nenhuma migração ou tabela de negócio criada nesta tarefa.

`git check-ignore .env` retornou `.env`. `git diff --check` passou.

## Workbench e revisão pendentes

Foram adicionadas as conexões `Time Marrom - desenvolvimento` e `Time Marrom - teste`, preservando a conexão preexistente e seu backup. Workbench foi aberto. Ainda é necessário abrir as duas conexões e conferir na interface com a senha local; não foi registrado teste visual de conexão. Anexar screenshot sem senhas e pedir revisão de outro integrante. Não declarar o cartão Pronto antes disso.
