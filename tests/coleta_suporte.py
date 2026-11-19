"""DI do ciclo durável nos testes SQLite; locks de processo têm testes MySQL."""

from tempfile import TemporaryDirectory
from app.coleta import bus_collector
from app.coleta.resiliencia import CicloResiliente
from app.repositories.coleta import ColetaRepository
from app.monitoramento.journal import JournalColeta


def criar_coletor_teste():
    pasta = TemporaryDirectory()
    journal = JournalColeta(pasta.name)
    repo = ColetaRepository()
    repo.assumir()
    servico = CicloResiliente(
        journal, repo, lambda: bus_collector.SPTransClient(), dormir=lambda _: None
    )
    coletor = bus_collector.BusCollector(servico)
    coletor._pasta_teste = pasta
    return coletor
