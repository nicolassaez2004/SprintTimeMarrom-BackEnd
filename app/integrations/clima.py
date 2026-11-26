"""ContextClient de clima (US06-T02): INMET horário e, se houver acesso, CGE.

Os clientes só leem e convertem a resposta da fonte; não calculam nada nem
gravam no banco (diagrama de componentes: "Clientes das fontes").

INMET (API apitempo): os horários `DT_MEDICAO` + `HR_MEDICAO` (HHMM) estão em
UTC, como informa o INMET. `CHUVA` é a precipitação acumulada na hora que
termina em `HR_MEDICAO`, em mm; `TEM_INS` é a temperatura instantânea, em °C.
Os valores chegam como texto e ausência chega como `null`. O sentinela -9999
(usado nos arquivos históricos do INMET) e valores fisicamente impossíveis
(chuva negativa, temperatura fora de -90..60 °C) viram NULL e ficam listados
em `descartados`: medição inválida nunca é gravada como valor válido.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

UTC = timezone.utc
INTERVALO_INMET_SEG = 3600
INTERVALO_CGE_SEG = 900
VERSAO_FONTE_INMET = "inmet-apitempo-horaria"
VERSAO_FONTE_INMET_ESTACOES = "inmet-apitempo-estacoes-T"
SENTINELA_AUSENTE = Decimal(-9999)
LIMITES = {"chuva": (Decimal(0), Decimal("99999.99")), "temperatura": (Decimal(-90), Decimal(60))}


class FonteClimaIndisponivel(RuntimeError):
    """A fonte não pode ser consultada (sem acesso, token ou resposta válida).

    A mensagem é curta e nunca contém a URL, que no INMET inclui o token.
    """


@dataclass(frozen=True)
class EstacaoFonte:
    codigo: str
    nome: str
    latitude: Decimal
    longitude: Decimal
    altitude_m: Decimal | None
    uf: str | None
    situacao: str | None
    operacao_inicio: date | None


@dataclass(frozen=True)
class ObservacaoFonte:
    codigo_estacao: str
    instante: datetime
    chuva_mm: Decimal | None
    temperatura_c: Decimal | None
    # Variáveis publicadas com valor inválido (sentinela ou fora do limite físico) e gravadas NULL.
    descartados: tuple = ()


def _decimal(valor):
    """Texto ou número da fonte → Decimal; `null`/vazio → None (não é zero)."""
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        numero = Decimal(texto)
    except InvalidOperation as erro:
        raise FonteClimaIndisponivel("valor numérico inválido na resposta do INMET") from erro
    if not numero.is_finite():
        raise FonteClimaIndisponivel("valor numérico inválido na resposta do INMET")
    return numero


def _medida(valor, variavel, descartados):
    """Decimal válido da variável ou None; inválido entra em `descartados`."""
    numero = _decimal(valor)
    if numero is None:
        return None
    minimo, maximo = LIMITES[variavel]
    if numero <= SENTINELA_AUSENTE or not minimo <= numero <= maximo:
        descartados.append(variavel)
        return None
    return numero


def _data(valor):
    texto = (valor or "").strip()[:10]
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def instante_inmet(dt_medicao, hr_medicao):
    """`2026-11-24` + `1300` (UTC) → datetime UTC."""
    try:
        dia = date.fromisoformat(str(dt_medicao).strip())
        hora = str(hr_medicao).strip().zfill(4)
        if len(hora) != 4 or not hora.isdigit():
            raise ValueError(hora)
        return datetime(dia.year, dia.month, dia.day, int(hora[:2]), int(hora[2:]), tzinfo=UTC)
    except (TypeError, ValueError) as erro:
        raise FonteClimaIndisponivel("data ou hora inválida na resposta do INMET") from erro


def converter_estacoes_inmet(corpo):
    if not isinstance(corpo, list):
        raise FonteClimaIndisponivel("lista de estações do INMET em formato inesperado")
    estacoes = []
    for item in corpo:
        if not isinstance(item, dict) or not item.get("CD_ESTACAO"):
            continue
        latitude, longitude = _decimal(item.get("VL_LATITUDE")), _decimal(item.get("VL_LONGITUDE"))
        if (latitude is None or longitude is None or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180 or (latitude == 0 and longitude == 0)):
            continue  # Sem coordenadas válidas não há como associar a trechos.
        estacoes.append(EstacaoFonte(
            codigo=str(item["CD_ESTACAO"]).strip(),
            nome=str(item.get("DC_NOME") or item["CD_ESTACAO"]).strip(),
            latitude=latitude, longitude=longitude,
            altitude_m=_decimal(item.get("VL_ALTITUDE")),
            uf=(item.get("SG_ESTADO") or item.get("UF") or None),
            situacao=(item.get("CD_SITUACAO") or None),
            operacao_inicio=_data(item.get("DT_INICIO_OPERACAO")),
        ))
    return estacoes


def converter_observacoes_inmet(corpo, codigo_esperado):
    if not isinstance(corpo, list):
        raise FonteClimaIndisponivel("observações do INMET em formato inesperado")
    observacoes = []
    for item in corpo:
        if not isinstance(item, dict):
            raise FonteClimaIndisponivel("observações do INMET em formato inesperado")
        codigo = str(item.get("CD_ESTACAO") or codigo_esperado).strip()
        if codigo != codigo_esperado:
            raise FonteClimaIndisponivel("o INMET respondeu com outra estação")
        descartados = []
        observacoes.append(ObservacaoFonte(
            codigo_estacao=codigo,
            instante=instante_inmet(item.get("DT_MEDICAO"), item.get("HR_MEDICAO")),
            chuva_mm=_medida(item.get("CHUVA"), "chuva", descartados),
            temperatura_c=_medida(item.get("TEM_INS"), "temperatura", descartados),
            descartados=tuple(descartados),
        ))
    return observacoes


class INMETClient:
    def __init__(self, base_url, token=None, timeout=(10, 60), sessao=None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or None
        self.timeout = timeout
        self.sessao = sessao or requests

    def _get(self, caminho):
        if not self.base_url:
            raise FonteClimaIndisponivel("INMET_API_URL não configurada")
        try:
            resposta = self.sessao.get(self.base_url + caminho, timeout=self.timeout)
            if resposta.status_code == 204:
                return []
            resposta.raise_for_status()
            return resposta.json()
        except requests.RequestException as erro:
            raise FonteClimaIndisponivel(f"INMET indisponível ({type(erro).__name__})") from erro
        except ValueError as erro:
            raise FonteClimaIndisponivel("resposta do INMET não é JSON") from erro

    def listar_estacoes_automaticas(self):
        return converter_estacoes_inmet(self._get("/estacoes/T"))

    def observacoes_horarias(self, codigo, inicio, fim):
        """Observações horárias de `inicio` a `fim` (datas UTC, inclusive)."""
        if not self.token:
            raise FonteClimaIndisponivel("INMET_TOKEN não configurado")
        caminho = f"/token/estacao/{inicio.isoformat()}/{fim.isoformat()}/{codigo}/{self.token}"
        return converter_observacoes_inmet(self._get(caminho), codigo)


class CGEClient:
    """CGE (15 min). Sem acesso acordado, só informa a indisponibilidade.

    O CGE não publica uma API aberta documentada. Enquanto a equipe não tiver
    acesso e o formato acordados, a coleta registra INDISPONIVEL e o caminho
    INMET segue normalmente.
    """

    def __init__(self, base_url=None):
        self.base_url = (base_url or "").rstrip("/")

    def observacoes(self, inicio, fim):
        if not self.base_url:
            raise FonteClimaIndisponivel("sem acesso à fonte CGE (CGE_API_URL não configurada)")
        raise FonteClimaIndisponivel("formato da fonte CGE ainda não acordado")
