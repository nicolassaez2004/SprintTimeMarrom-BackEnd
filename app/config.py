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
    # Banco de dados
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # API da SPTrans
    SPTRANS_API_URL = os.getenv(
        "SPTRANS_API_URL",
        "https://api.olhovivo.sptrans.com.br/v2.1"
    )

    SPTRANS_TOKEN = os.getenv("SPTRANS_TOKEN")

    # O processo coletor usa 30 s por padrão e aceita intervalo de 15 a 60 s.
    GPS_COLLECTION_INTERVAL_SECONDS = validar_intervalo_coleta(
        os.getenv("GPS_COLLECTION_INTERVAL_SECONDS", "30")
    )
