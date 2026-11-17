import os

from dotenv import load_dotenv

# Carrega as variaveis do arquivo .env
load_dotenv()


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