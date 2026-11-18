import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import create_app
from app.config import validar_intervalo_coleta
from app.models import ExecucaoColeta
from app.coleta import bus_collector


class RelogioFalso:
    def __init__(self):
        self.agora = 0.0

    def monotonic(self):
        return self.agora

    def dormir(self, segundos):
        self.agora += segundos


class SessaoFalsa:
    def __init__(self):
        self.added = []
        self.add_all = Mock()
        self.commit = Mock()
        self.flush = Mock()
        self.rollback = Mock()
        self.remove = Mock()
        # Consulta de posições já gravadas (deduplicação): nenhuma por padrão.
        self.query = Mock()
        self.query.return_value.filter.return_value.all.return_value = []

    def add(self, objeto):
        self.added.append(objeto)
        if isinstance(objeto, ExecucaoColeta):
            objeto.id = 77


class TestGpsCollector(unittest.TestCase):
    def setUp(self):
        self.session = SessaoFalsa()
        self.db_patcher = patch.object(
            bus_collector, "db", SimpleNamespace(session=self.session)
        )
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    def test_intervalo_padrao_e_30_segundos(self):
        self.assertEqual(bus_collector.INTERVALO_COLETA_SEGUNDOS, 30)

    def test_intervalos_15_30_60_sao_validos_e_fora_da_faixa_rejeitados(self):
        for intervalo in (15, 30, 60):
            self.assertEqual(validar_intervalo_coleta(intervalo), intervalo)
        for intervalo in (14, 61, "abc"):
            with self.subTest(intervalo=intervalo):
                with self.assertRaises(ValueError):
                    validar_intervalo_coleta(intervalo)

    def test_relogio_controlado_comprova_cadencia_de_15_30_e_60_segundos(self):
        for intervalo in (15, 30, 60):
            with self.subTest(intervalo=intervalo):
                relogio = RelogioFalso()
                inicios = []

                def coletar(prevista_em):
                    self.assertIsNotNone(prevista_em.tzinfo)
                    inicios.append(relogio.monotonic())
                    relogio.agora += 0.25

                bus_collector.executar_coletor(
                    intervalo,
                    coletar=coletar,
                    relogio=relogio.monotonic,
                    dormir=relogio.dormir,
                    max_ciclos=4,
                )
                self.assertEqual(inicios, [0, intervalo, intervalo * 2, intervalo * 3])

    def test_ciclo_consulta_linha_uma_vez_grava_selecionados_e_contabiliza_execucao(self):
        selecionados = [
            SimpleNamespace(prefixo="5810", codigo_linha=123),
            SimpleNamespace(prefixo="5811", codigo_linha=123),
        ]
        resposta = {
            "hr": "12:00",
            "vs": [
                {"p": "5810", "py": -23.5, "px": -46.6, "ta": "2026-11-17T15:00:00Z"},
                {"p": "fora-selecao", "py": -23.4, "px": -46.5},
                {"p": "5811", "py": -23.3, "px": -46.4, "ta": "2026-11-17T15:00:05Z"},
                {"p": "5810", "py": None, "px": -46.2},
            ],
        }
        cliente = SimpleNamespace(buscar_posicoes=Mock(return_value=resposta))
        modelo_selecao = SimpleNamespace(
            query=SimpleNamespace(all=Mock(return_value=selecionados))
        )

        with (
            patch.object(bus_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(bus_collector, "SPTransClient", return_value=cliente),
        ):
            total = bus_collector.BusCollector().coletar_ciclo()

        self.assertEqual(total, 3)
        cliente.buscar_posicoes.assert_called_once_with(123)
        self.session.add_all.assert_called_once()
        # Um commit registra o início do ciclo; outro grava o resultado.
        self.assertEqual(self.session.commit.call_count, 2)
        execucoes = [obj for obj in self.session.added if isinstance(obj, ExecucaoColeta)]
        self.assertEqual(len(execucoes), 1)
        self.assertEqual(execucoes[0].estado, "OK")
        self.assertEqual(execucoes[0].recebidos, 3)
        self.assertEqual(execucoes[0].novos_persistidos, 3)
        registros = self.session.add_all.call_args.args[0]
        self.assertEqual(
            [(r.veiculo_prefixo, r.prefixo_origem, r.linha_origem, r.latitude, r.longitude)
             for r in registros],
            [("5810", "5810", "123", -23.5, -46.6), ("5811", "5811", "123", -23.3, -46.4),
             ("5810", "5810", "123", None, -46.2)],
        )
        self.assertEqual(
            registros[0].horario_gps, datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
        )
        self.assertIsNone(registros[2].horario_gps)
        self.assertTrue(all(r.coletado_em.tzinfo is not None for r in registros))
        # O objeto original da API é preservado, inclusive o campo inválido.
        self.assertEqual(registros[2].payload, {"p": "5810", "py": None, "px": -46.2})
        self.assertTrue(all(r.execucao_id == 77 for r in registros))

    def test_falha_no_meio_do_ciclo_fica_registrada_como_falha(self):
        selecionados = [SimpleNamespace(prefixo="5810", codigo_linha=123)]
        modelo_selecao = SimpleNamespace(
            query=SimpleNamespace(all=Mock(return_value=selecionados))
        )
        with (
            patch.object(bus_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(bus_collector, "SPTransClient", side_effect=RuntimeError("token")),
        ):
            total = bus_collector.BusCollector().coletar_ciclo()

        self.assertEqual(total, 0)
        self.session.rollback.assert_called_once_with()
        execucao = self.session.added[0]
        self.assertEqual(execucao.estado, "FALHA")
        self.assertEqual(execucao.erro, "RuntimeError")
        self.assertIsNotNone(execucao.finalizada_em)

    def test_falha_em_todas_as_linhas_e_falha_e_em_algumas_e_parcial(self):
        selecionados = [
            SimpleNamespace(prefixo="1", codigo_linha=1),
            SimpleNamespace(prefixo="2", codigo_linha=2),
        ]
        modelo_selecao = SimpleNamespace(
            query=SimpleNamespace(all=Mock(return_value=selecionados))
        )
        casos = {
            "FALHA": [RuntimeError(), RuntimeError()],
            "PARCIAL": [RuntimeError(), {"vs": []}],
        }
        for esperado, respostas in casos.items():
            with self.subTest(esperado=esperado):
                self.session.added.clear()
                cliente = SimpleNamespace(buscar_posicoes=Mock(side_effect=respostas))
                with (
                    patch.object(bus_collector, "OnibusSelecionado", modelo_selecao),
                    patch.object(bus_collector, "SPTransClient", return_value=cliente),
                ):
                    bus_collector.BusCollector().coletar_ciclo()
                self.assertEqual(self.session.added[0].estado, esperado)

    def test_ciclo_sem_onibus_contabiliza_execucao_sem_consultar_api(self):
        modelo_selecao = SimpleNamespace(query=SimpleNamespace(all=Mock(return_value=[])))
        with (
            patch.object(bus_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(bus_collector, "SPTransClient") as cliente,
        ):
            total = bus_collector.BusCollector().coletar_ciclo()

        self.assertEqual(total, 0)
        cliente.assert_not_called()
        self.session.add_all.assert_not_called()
        self.assertEqual(self.session.added[0].estado, "OK")
        self.assertEqual(self.session.added[0].recebidos, 0)

    def test_cada_execucao_remove_sua_sessao_sql(self):
        relogio = RelogioFalso()
        bus_collector.executar_coletor(
            15,
            coletar=lambda _: setattr(relogio, "agora", relogio.agora + 0.1),
            relogio=relogio.monotonic,
            dormir=relogio.dormir,
            max_ciclos=3,
        )
        self.assertEqual(self.session.remove.call_count, 3)

    def test_coletor_para_quando_o_lock_e_perdido(self):
        relogio = RelogioFalso()
        coletar = Mock()
        estados = iter([True, True, False])
        ciclos = bus_collector.executar_coletor(
            30,
            coletar=coletar,
            relogio=relogio.monotonic,
            dormir=relogio.dormir,
            manter_ativo=lambda: next(estados),
            max_ciclos=10,
        )
        self.assertEqual(ciclos, 2)
        self.assertEqual(coletar.call_count, 2)

    def test_posicao_ja_gravada_ou_repetida_no_ciclo_nao_e_duplicada(self):
        horario = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
        self.session.query.return_value.filter.return_value.all.return_value = [
            ("5810", horario)
        ]
        selecionados = [
            SimpleNamespace(prefixo="5810", codigo_linha=123),
            SimpleNamespace(prefixo="5811", codigo_linha=123),
        ]
        resposta = {"vs": [
            {"p": "5810", "py": -23.5, "px": -46.6, "ta": "2026-11-17T15:00:00Z"},
            {"p": "5811", "py": -23.3, "px": -46.4, "ta": "2026-11-17T15:00:05Z"},
            {"p": "5811", "py": -23.3, "px": -46.4, "ta": "2026-11-17T15:00:05Z"},
        ]}
        cliente = SimpleNamespace(buscar_posicoes=Mock(return_value=resposta))
        modelo_selecao = SimpleNamespace(
            query=SimpleNamespace(all=Mock(return_value=selecionados))
        )
        with (
            patch.object(bus_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(bus_collector, "SPTransClient", return_value=cliente),
        ):
            total = bus_collector.BusCollector().coletar_ciclo()

        self.assertEqual(total, 1)
        execucao = self.session.added[0]
        self.assertEqual((execucao.recebidos, execucao.novos_persistidos), (3, 1))
        novos = self.session.add_all.call_args.args[0]
        self.assertEqual([r.prefixo_origem for r in novos], ["5811"])

    def test_sem_chave_completa_preserva_todos_os_registros_com_nulos(self):
        selecionados = [SimpleNamespace(prefixo="5810", codigo_linha=123)]
        resposta = {"vs": [
            {"p": "5810", "py": -23.5, "px": -46.6},
            {"p": "5810", "py": -23.5, "px": -46.6},  # repetido: mesmo objeto
            {"p": "5810", "py": -23.6, "px": -46.6},  # diferente: preservado
        ]}
        cliente = SimpleNamespace(buscar_posicoes=Mock(return_value=resposta))
        modelo_selecao = SimpleNamespace(
            query=SimpleNamespace(all=Mock(return_value=selecionados))
        )
        with (
            patch.object(bus_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(bus_collector, "SPTransClient", return_value=cliente),
        ):
            total = bus_collector.BusCollector().coletar_ciclo()

        self.assertEqual(total, 3)
        # Sem horário não há consulta a ciclos anteriores.
        self.session.query.assert_not_called()

    def test_lock_singleton_usa_get_lock_no_mysql(self):
        conexao = Mock()
        conexao.execute.return_value.scalar.side_effect = [1, 0, 1]  # adquire, recusa, libera
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="mysql"), connect=Mock(return_value=conexao)
        )
        primeiro = bus_collector.LockColetor(engine)
        self.assertTrue(primeiro.adquirir())
        self.assertIn("GET_LOCK", str(conexao.execute.call_args.args[0]))

        # Segundo coletor no mesmo banco não obtém o lock e libera a conexão.
        segundo = bus_collector.LockColetor(engine)
        self.assertFalse(segundo.adquirir())
        self.assertIsNone(segundo.conexao)

    def test_lock_singleton_rejeita_banco_sem_suporte(self):
        engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
        with self.assertRaises(RuntimeError):
            bus_collector.LockColetor(engine)

    def test_criar_app_flask_nao_inicia_coletor(self):
        with patch.object(bus_collector, "executar_coletor") as executar:
            app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.assertIsNotNone(app)
        executar.assert_not_called()

    def test_diagnostico_por_prefixo_imprime_posicao_sem_gravar(self):
        resposta = {
            "hr": "12:00",
            "l": [{"c": "5810-10", "cl": 123,
                   "vs": [{"p": 5810, "py": -23.5, "px": -46.6, "ta": "12:00:00Z"}]}],
        }
        cliente = SimpleNamespace(buscar_todas_posicoes=Mock(return_value=resposta))
        saida = io.StringIO()
        with patch.object(bus_collector, "SPTransClient", return_value=cliente):
            with redirect_stdout(saida):
                bus_collector.imprimir_localizacao_atual("5810")
        self.assertIn("Latitude: -23.5", saida.getvalue())
        self.assertIn("Longitude: -46.6", saida.getvalue())
        self.session.add_all.assert_not_called()

    def test_diagnostico_por_linha_imprime_veiculos(self):
        cliente = SimpleNamespace(
            buscar_linhas=Mock(return_value=[{"cl": 123, "lt": "5810", "sl": 1}]),
            buscar_posicoes=Mock(return_value={"hr": "12:00", "vs": [
                {"p": "9999", "py": -23.5, "px": -46.6, "ta": "12:00:00Z"}
            ]}),
        )
        saida = io.StringIO()
        with patch.object(bus_collector, "SPTransClient", return_value=cliente):
            with redirect_stdout(saida):
                bus_collector.imprimir_posicoes_da_linha("5810")
        cliente.buscar_posicoes.assert_called_once_with(123)
        self.assertIn("Prefixo: 9999", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
