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
    # Sem lista explícita, cabeçalhos de IP são ignorados. Separar CIDRs por vírgula.
    INDICADORES_TRUSTED_PROXY_CIDRS = os.getenv("INDICADORES_TRUSTED_PROXY_CIDRS") or ""
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
    # Manifesto para snapshots GeoJSON mensais; consulte data/infraestrutura/README.md.
    INFRAESTRUTURA_SNAPSHOT_MANIFEST = os.getenv("INFRAESTRUTURA_SNAPSHOT_MANIFEST") or None
    PROGRAMACAO_STORAGE_DIR = os.getenv("PROGRAMACAO_STORAGE_DIR") or None
    DOCUMENTOS_STORAGE_DIR = os.getenv("DOCUMENTOS_STORAGE_DIR") or None
    EXPORTACOES_DIR = os.getenv("EXPORTACOES_DIR") or None

    # O processo coletor usa 30 s por padrão e aceita intervalo de 15 a 60 s.
    # Valor vazio no .env (como em .env.example) também usa o padrão.
    GPS_COLLECTION_INTERVAL_SECONDS = validar_intervalo_coleta(
        os.getenv("GPS_COLLECTION_INTERVAL_SECONDS") or "30"
    )

    # US06: clima por trecho. Token do INMET (pedido por e-mail ao INMET) e
    # acesso ao CGE ficam só no .env; sem eles a coleta registra INDISPONIVEL.
    INMET_API_URL = os.getenv("INMET_API_URL") or "https://apitempo.inmet.gov.br"
    INMET_TOKEN = os.getenv("INMET_TOKEN") or None
    CGE_API_URL = os.getenv("CGE_API_URL") or None
    # Estações do catálogo INMET consideradas (km do centro de São Paulo).
    CLIMA_RAIO_CATALOGO_KM = os.getenv("CLIMA_RAIO_CATALOGO_KM") or "60"
    # Distância máxima trecho → estação para usar a medição (decisão provisória
    # da US06-T03, pendente do PO). Além dela o vínculo fica FORA_DO_RAIO.
    CLIMA_RAIO_MAXIMO_M = os.getenv("CLIMA_RAIO_MAXIMO_M") or "25000"

    # US15-T01: lacuna máxima (s) entre as duas posições usadas para estimar
    # uma passagem. Padrão adotado em 24/11/2026: 120 s (4 ciclos de 30 s), o
    # valor conferido nos 20 casos da US15-T04 (docs/US15-T01.md).
    PASSAGEM_LACUNA_MAXIMA_SEG = os.getenv("PASSAGEM_LACUNA_MAXIMA_SEG") or "120"
