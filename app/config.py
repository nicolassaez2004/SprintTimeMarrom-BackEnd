import os

from dotenv import load_dotenv

# Carrega as variaveis do arquivo .env
load_dotenv()


def validar_intervalo_coleta(valor):
    try:
        intervalo = int(valor)
    except (TypeError, ValueError) as error:
        raise ValueError("GPS_COLLECTION_INTERVAL_SECONDS deve ser um inteiro") from error
    if not 15 <= intervalo <= 60:
        raise ValueError("GPS_COLLECTION_INTERVAL_SECONDS deve estar entre 15 e 60")
    return intervalo


class Config:
    COLETA_STATE_DIR = os.getenv('COLETA_STATE_DIR') or None
    # Webhook (Discord/Slack/Teams) dos alertas da coleta; vazio = caixa interna.
    ALERTA_WEBHOOK_URL = os.getenv('ALERTA_WEBHOOK_URL') or None
    # Banco de dados
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Certificado CA local para serviços MySQL remotos (por exemplo, Aiven).
    MYSQL_SSL_CA = os.getenv("MYSQL_SSL_CA")
    MUNICIPIO_GEOJSON = os.getenv("MUNICIPIO_GEOJSON")

    # API da SPTrans
    SPTRANS_API_URL = os.getenv(
        "SPTRANS_API_URL",
        "https://api.olhovivo.sptrans.com.br/v2.1"
    )

    SPTRANS_TOKEN = os.getenv("SPTRANS_TOKEN")

    # Endpoint de download GTFS da SPTrans. Pode ser substituído por ambiente.
    GTFS_SOURCE_URL = os.getenv("GTFS_SOURCE_URL") or (
        "https://www.sptrans.com.br/umbraco/Surface/PerfilDesenvolvedor/"
        "BaixarGTFS?memberName=sptrans"
    )
    GTFS_STORAGE_DIR = os.getenv("GTFS_STORAGE_DIR") or None
    GTFS_TASK_LOG_FILE = os.getenv("GTFS_TASK_LOG_FILE") or None
    PROGRAMACAO_STORAGE_DIR = os.getenv("PROGRAMACAO_STORAGE_DIR") or None
    DOCUMENTOS_STORAGE_DIR = os.getenv("DOCUMENTOS_STORAGE_DIR") or None

    # O processo coletor usa 30 s por padrão e aceita intervalo de 15 a 60 s.
    # Valor vazio no .env (como em .env.example) também usa o padrão.
    GPS_COLLECTION_INTERVAL_SECONDS = validar_intervalo_coleta(
        os.getenv("GPS_COLLECTION_INTERVAL_SECONDS") or "30"
    )
