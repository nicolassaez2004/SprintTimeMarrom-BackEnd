"""Captura, valida e versiona o ZIP original do GTFS por SHA-256."""

import csv
import math
import os
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.integrations.gtfs import GTFSClient
from app.models import ArquivoArmazenado, Linha, Parada, Trecho, VersaoGTFS
from app.storage import FileStore, hash_arquivo


class GTFSValidationError(ValueError):
    """O download terminou, mas não contém um feed GTFS íntegro."""


class VersaoGTFSInexistente(LookupError):
    """Nenhuma versão GTFS cobre a data de operação pedida."""


class VersaoGTFSAmbigua(LookupError):
    """Mais de uma versão GTFS cobre a data; a escolha não é feita em silêncio."""

    def __init__(self, data_operacao, versoes):
        self.data_operacao = data_operacao
        self.versoes = versoes
        ids = ", ".join(v.id for v in versoes)
        super().__init__(f"{len(versoes)} versões GTFS vigentes em {data_operacao}: {ids}")


def versao_vigente(data_operacao):
    """Versão GTFS cuja vigência [início, fim] contém a data de operação.

    Usa apenas `vigencia_inicio`/`vigencia_fim` gravados na captura (US02-T01);
    `vigencia_fim` NULL significa vigência aberta. Somente leitura: não altera
    versões nem observações já ligadas a uma versão.
    """
    if isinstance(data_operacao, datetime) or not isinstance(data_operacao, date):
        # Converter instante GPS em data operacional depende de fuso e de
        # viagens após 24h: o chamador deve informar a data já definida.
        raise TypeError("data_operacao deve ser uma date, não um instante")
    versoes = (
        VersaoGTFS.query
        .filter(VersaoGTFS.vigencia_inicio <= data_operacao)
        .filter(db.or_(VersaoGTFS.vigencia_fim.is_(None),
                       VersaoGTFS.vigencia_fim >= data_operacao))
        .order_by(VersaoGTFS.vigencia_inicio, VersaoGTFS.capturada_em, VersaoGTFS.id)
        .all()
    )
    if not versoes:
        raise VersaoGTFSInexistente(f"nenhuma versão GTFS vigente em {data_operacao}")
    if len(versoes) > 1:
        raise VersaoGTFSAmbigua(data_operacao, versoes)
    return versoes[0]


@dataclass(frozen=True)
class ResultadoCapturaGTFS:
    versao: VersaoGTFS
    nova: bool
    identificada_em: datetime
    persistida_em: datetime | None

    @property
    def tempo_ate_base_segundos(self):
        if self.persistida_em is None:
            return None
        return (self.persistida_em - self.identificada_em).total_seconds()


def _validar_zip(caminho):
    try:
        with zipfile.ZipFile(caminho) as arquivo:
            if arquivo.testzip() is not None:
                raise GTFSValidationError("ZIP GTFS contém entrada corrompida")
            nomes = {Path(item.filename).name.lower() for item in arquivo.infolist()}
            obrigatorios = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
            faltantes = obrigatorios - nomes
            if faltantes or not ({"calendar.txt", "calendar_dates.txt"} & nomes):
                raise GTFSValidationError("ZIP não contém os arquivos básicos de um feed GTFS")
            candidatos = [item for item in arquivo.namelist() if Path(item).name.lower() == "feed_info.txt"]
            if candidatos:
                with arquivo.open(candidatos[0]) as stream:
                    linhas = list(csv.DictReader(line.decode("utf-8-sig") for line in stream))
                if not linhas:
                    raise GTFSValidationError("feed_info.txt está vazio")
                info = linhas[0]
                inicio = _ler_data_gtfs(info.get("feed_start_date"))
                fim = _ler_data_gtfs(info.get("feed_end_date"))
                if inicio and fim and fim < inicio:
                    raise GTFSValidationError("vigência GTFS termina antes do início")
                return inicio, fim
    except (zipfile.BadZipFile, OSError, UnicodeError, csv.Error, KeyError, ValueError) as erro:
        if isinstance(erro, GTFSValidationError):
            raise
        raise GTFSValidationError("download não é um ZIP GTFS íntegro") from erro
    return None, None


def _ler_data_gtfs(valor):
    if not valor:
        return None
    return datetime.strptime(valor, "%Y%m%d").date()


def _diretorio_gtfs(diretorio=None):
    return Path(diretorio or current_app.config.get("GTFS_STORAGE_DIR")
                or (Path(current_app.instance_path) / "gtfs"))


def capturar_gtfs(*, url=None, diretorio=None, cliente=None, agora=None):
    """Baixa e guarda uma captura imutável; hash repetido retorna a versão atual.

    O arquivo é validado antes de entrar no armazenamento permanente. A versão
    e os metadados do arquivo são confirmados em uma única transação SQL.
    """
    return capturar_gtfs_com_resultado(
        url=url, diretorio=diretorio, cliente=cliente, agora=agora,
    ).versao


def capturar_gtfs_com_resultado(*, url=None, diretorio=None, cliente=None,
                              agora=None, relogio=None, ao_identificar=None):
    """Inclui identificação do hash e confirmação do commit, sem alterar a API antiga.

    Identificação é o instante em que o ZIP válido foi baixado e teve seu hash
    calculado; não representa a data de publicação desconhecida da fonte.
    Uma repetição não tem novo instante de persistência.
    """
    relogio = relogio or (lambda: datetime.now(timezone.utc))
    url = url or current_app.config.get("GTFS_SOURCE_URL")
    diretorio = _diretorio_gtfs(diretorio)
    cliente = cliente or GTFSClient()
    agora = agora or relogio()
    if agora.tzinfo is None:
        raise ValueError("data de captura deve conter fuso horário")
    armazenamento = FileStore(diretorio)

    fd, temporario = tempfile.mkstemp(prefix="gtfs-", suffix=".part", dir=diretorio)
    os.close(fd)
    caminho_temp = Path(temporario)
    try:
        with caminho_temp.open("wb") as destino:
            cliente.baixar_para(url, destino)
            destino.flush()
            os.fsync(destino.fileno())

        inicio_feed, fim_feed = _validar_zip(caminho_temp)
        digest, tamanho = hash_arquivo(caminho_temp)
        identificada_em = relogio()
        if identificada_em.tzinfo is None:
            raise ValueError("data de identificação deve conter fuso horário")
        if ao_identificar is not None:
            # O agendador persiste a primeira identificação antes de publicar;
            # retomadas do mesmo hash não reiniciam a medição do prazo.
            identificada_em = ao_identificar(digest, identificada_em)
        # Publicar/verificar antes de reutilizar metadados, inclusive quando
        # outra máquina registrou o hash ou o ZIP local precisa ser recuperado.
        chave = armazenamento.publicar_gtfs(caminho_temp, digest, tamanho)
        existente = VersaoGTFS.query.filter_by(hash=digest).first()
        if existente:
            return ResultadoCapturaGTFS(existente, False, identificada_em, None)

        arquivo = ArquivoArmazenado(
            id=str(uuid4()), chave=chave, nome_original="feed.zip",
            mime_type="application/zip", tamanho_bytes=tamanho,
            sha256=digest, criado_em=agora,
        )
        versao = VersaoGTFS(
            id=str(uuid4()), capturada_em=agora, hash=digest,
            vigencia_inicio=inicio_feed or agora.astimezone(timezone.utc).date(),
            vigencia_fim=fim_feed, arquivo=arquivo,
        )
        try:
            db.session.add(arquivo)
            db.session.add(versao)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            repetida = VersaoGTFS.query.filter_by(hash=digest).first()
            if repetida:
                return ResultadoCapturaGTFS(repetida, False, identificada_em, None)
            raise
        return ResultadoCapturaGTFS(versao, True, identificada_em, relogio())
    except Exception:
        db.session.rollback()
        # O objeto final pode estar em uso por outra transação. Preservá-lo
        # permite retomada; somente o temporário privado é removido abaixo.
        raise
    finally:
        caminho_temp.unlink(missing_ok=True)


def _abrir_versao(versao_id, diretorio):
    """Abre o ZIP original da versão, conferindo que é o conteúdo do hash."""
    versao = db.session.get(VersaoGTFS, versao_id) if versao_id else None
    if versao is None:
        raise LookupError(f"versão GTFS inexistente: {versao_id!r}")
    try:
        caminho = FileStore(_diretorio_gtfs(diretorio)).caminho_gtfs(
            versao.arquivo.chave, versao.hash)
    except FileNotFoundError as erro:
        raise GTFSValidationError(str(erro)) from erro
    return versao, zipfile.ZipFile(caminho)


def _ler_csv(arquivo, nome, obrigatorio=True):
    """Lê um .txt do feed como dicionários; arquivo opcional ausente -> []."""
    candidatos = [i for i in arquivo.namelist() if Path(i).name.lower() == nome]
    if not candidatos:
        if obrigatorio:
            raise GTFSValidationError(f"{nome} ausente no feed")
        return
    with arquivo.open(candidatos[0]) as stream:
        texto = (linha.decode("utf-8-sig") for linha in stream)
        for numero, linha in enumerate(csv.DictReader(texto), start=2):
            yield numero, {k.strip(): (v or "").strip() for k, v in linha.items() if k}


def _unicos(registros, nome, chave):
    vistos = {}
    for numero, linha in registros:
        codigo = linha.get(chave)
        if not codigo:
            raise GTFSValidationError(f"{nome} linha {numero}: {chave} vazio")
        if codigo in vistos:
            raise GTFSValidationError(f"{nome}: {chave} {codigo!r} repetido")
        vistos[codigo] = (numero, linha)
    return vistos


def _coordenada(valor, limite, nome, numero, arquivo="stops.txt"):
    """Decimal exato do texto GTFS, arredondado a 8 casas (coluna DECIMAL(11,8))."""
    try:
        coordenada = Decimal(valor).quantize(Decimal("1e-8"))
    except (InvalidOperation, ValueError):
        coordenada = None
    if coordenada is None or not -limite <= coordenada <= limite:
        raise GTFSValidationError(f"{arquivo} linha {numero}: {nome} inválida")
    return coordenada


def importar_linhas_paradas(versao_id, *, diretorio=None):
    """Importa `routes.txt` e `stops.txt` do ZIP da versão, numa transação.

    Reimportar a mesma versão não duplica registros. Códigos iguais em versões
    diferentes geram registros distintos, cada um ligado à sua versão.
    """
    versao, arquivo = _abrir_versao(versao_id, diretorio)
    with arquivo:
        if Linha.query.filter_by(versao_gtfs_id=versao.id).first():
            return {"linhas": Linha.query.filter_by(versao_gtfs_id=versao.id).count(),
                    "paradas": Parada.query.filter_by(versao_gtfs_id=versao.id).count()}
        rotas = _unicos(_ler_csv(arquivo, "routes.txt"), "routes.txt", "route_id")
        paradas = _unicos(_ler_csv(arquivo, "stops.txt"), "stops.txt", "stop_id")

    linhas = []
    for codigo, (numero, rota) in rotas.items():
        nome = rota.get("route_long_name") or rota.get("route_short_name")
        if not nome:
            raise GTFSValidationError(f"routes.txt linha {numero}: linha sem nome")
        linhas.append(Linha(versao_gtfs_id=versao.id, codigo=codigo, nome=nome))
    registros = [
        Parada(
            versao_gtfs_id=versao.id, codigo=codigo, nome=parada.get("stop_name", ""),
            latitude=_coordenada(parada.get("stop_lat", ""), 90, "stop_lat", numero),
            longitude=_coordenada(parada.get("stop_lon", ""), 180, "stop_lon", numero),
        )
        for codigo, (numero, parada) in paradas.items()
    ]
    try:
        db.session.add_all(linhas + registros)
        db.session.commit()
    except IntegrityError:
        # Outra importação simultânea da mesma versão pode ter vencido.
        db.session.rollback()
        if not Linha.query.filter_by(versao_gtfs_id=versao.id).first():
            raise
    except Exception:
        db.session.rollback()
        raise
    return {"linhas": Linha.query.filter_by(versao_gtfs_id=versao.id).count(),
            "paradas": Parada.query.filter_by(versao_gtfs_id=versao.id).count()}


def _inteiro(valor, nome, arquivo, numero):
    try:
        return int(valor)
    except ValueError:
        raise GTFSValidationError(f"{arquivo} linha {numero}: {nome} inválido") from None


def _indices_no_tracado(coordenadas, tracado):
    """Ponto do traçado para cada parada, sem retroceder no traçado.

    Minimiza a soma das distâncias (programação dinâmica), o que evita que
    uma linha circular associe a primeira parada ao fim do traçado.
    """
    custo_anterior = [0.0] * len(tracado)
    escolhas = []
    for lon, lat in coordenadas:
        melhor, melhor_k, custo, origem = math.inf, 0, [], []
        for k, (x, y) in enumerate(tracado):
            if custo_anterior[k] < melhor:
                melhor, melhor_k = custo_anterior[k], k
            custo.append(melhor + (x - lon) ** 2 + (y - lat) ** 2)
            origem.append(melhor_k)
        escolhas.append(origem)
        custo_anterior = custo
    k = min(range(len(tracado)), key=custo_anterior.__getitem__)
    indices = []
    for origem in reversed(escolhas):
        indices.append(k)
        k = origem[k]
    return indices[::-1]


def importar_trechos(versao_id, *, diretorio=None):
    """Importa os trechos entre paradas consecutivas de cada trajeto GTFS.

    Viagens (`trips.txt`) com a mesma linha, sentido e sequência de paradas
    (`stop_times.txt`) formam um trajeto, identificado pelo menor `trip_id`.
    Sequências diferentes nunca se misturam. A geometria vem de `shapes.txt`
    quando houver; senão, é a reta entre as duas paradas. Depende de
    `importar_linhas_paradas` para a mesma versão.
    """
    versao, arquivo = _abrir_versao(versao_id, diretorio)
    existentes = Trecho.query.filter_by(versao_gtfs_id=versao.id)
    with arquivo:
        if existentes.first():
            return {"trechos": existentes.count()}
        linhas = {codigo for (codigo,) in
                  db.session.query(Linha.codigo).filter_by(versao_gtfs_id=versao.id)}
        if not linhas:
            raise GTFSValidationError("importe linhas e paradas desta versão antes dos trechos")
        paradas = {codigo: (float(lon), float(lat)) for codigo, lon, lat in db.session.query(
            Parada.codigo, Parada.longitude, Parada.latitude).filter_by(versao_gtfs_id=versao.id)}

        viagens = {}
        for numero, viagem in _unicos(_ler_csv(arquivo, "trips.txt"), "trips.txt",
                                      "trip_id").values():
            if viagem.get("route_id") not in linhas:
                raise GTFSValidationError(f"trips.txt linha {numero}: linha inexistente")
            if not viagem.get("direction_id"):
                raise GTFSValidationError(f"trips.txt linha {numero}: direction_id vazio")
            viagens[viagem["trip_id"]] = (
                viagem["route_id"], viagem["direction_id"], viagem.get("shape_id") or None,
            )

        sequencias = defaultdict(dict)
        for numero, horario in _ler_csv(arquivo, "stop_times.txt"):
            trip_id, stop_id = horario.get("trip_id"), horario.get("stop_id")
            if trip_id not in viagens:
                raise GTFSValidationError(f"stop_times.txt linha {numero}: viagem inexistente")
            if stop_id not in paradas:
                raise GTFSValidationError(f"stop_times.txt linha {numero}: parada inexistente")
            ordem = _inteiro(horario.get("stop_sequence", ""), "stop_sequence",
                             "stop_times.txt", numero)
            if ordem in sequencias[trip_id]:
                raise GTFSValidationError(f"stop_times.txt linha {numero}: stop_sequence repetido")
            sequencias[trip_id][ordem] = stop_id

        trajetos = {}
        for trip_id in sorted(sequencias):
            linha, sentido, shape = viagens[trip_id]
            sequencia = tuple(sequencias[trip_id][k] for k in sorted(sequencias[trip_id]))
            trajetos.setdefault((linha, sentido, sequencia), (trip_id, shape))

        necessarios = {shape for _, shape in trajetos.values() if shape}
        pontos = defaultdict(list)
        for numero, ponto in _ler_csv(arquivo, "shapes.txt", obrigatorio=False):
            if ponto.get("shape_id") in necessarios:
                pontos[ponto["shape_id"]].append((
                    _inteiro(ponto.get("shape_pt_sequence", ""), "shape_pt_sequence",
                             "shapes.txt", numero),
                    float(_coordenada(ponto.get("shape_pt_lon", ""), 180, "shape_pt_lon",
                                      numero, "shapes.txt")),
                    float(_coordenada(ponto.get("shape_pt_lat", ""), 90, "shape_pt_lat",
                                      numero, "shapes.txt")),
                ))

    trechos = []
    for (linha, sentido, sequencia), (trip_id, shape) in trajetos.items():
        coordenadas = [paradas[codigo] for codigo in sequencia]
        tracado = [(lon, lat) for _, lon, lat in sorted(pontos.get(shape, []))]
        indices = _indices_no_tracado(coordenadas, tracado) if len(tracado) > 1 else None
        for i in range(len(sequencia) - 1):
            segmento = tracado[indices[i]:indices[i + 1] + 1] if indices else []
            if len(segmento) < 2:
                segmento = coordenadas[i:i + 2]
            trechos.append(dict(
                id=str(uuid4()),
                versao_gtfs_id=versao.id, linha_codigo=linha, sentido=sentido,
                trajeto=trip_id, ordem=i + 1,
                parada_origem_codigo=sequencia[i], parada_destino_codigo=sequencia[i + 1],
                geometria=[list(ponto) for ponto in segmento],
            ))
    try:
        # VALUES explícitos agrupam ST_GeomFromText no mesmo comando. O
        # executemany do PyMySQL pode executar uma ida à nuvem por geometria.
        for inicio in range(0, len(trechos), 250):
            db.session.execute(Trecho.__table__.insert().values(trechos[inicio:inicio + 250]))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if not existentes.first():
            raise
    except Exception:
        db.session.rollback()
        raise
    return {"trechos": existentes.count()}
