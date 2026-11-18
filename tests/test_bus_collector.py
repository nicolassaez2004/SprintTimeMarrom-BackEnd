import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import create_app
from app.config import validar_intervalo_coleta
from app.models import ExecucaoColeta
from app.services import gps_collector


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

    def add(self, objeto):
        self.added.append(objeto)
        if isinstance(objeto, ExecucaoColeta):
            objeto.id = 77


class TestGpsCollector(unittest.TestCase):
    def setUp(self):
        self.session = SessaoFalsa()
        self.db_patcher = patch.object(
            gps_collector, "db", SimpleNamespace(session=self.session)
        )
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    def test_intervalo_padrao_e_30_segundos(self):
        self.assertEqual(gps_collector.INTERVALO_COLETA_SEGUNDOS, 30)

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

                def coletar():
                    inicios.append(relogio.monotonic())
                    relogio.agora += 0.25

                gps_collector.executar_coletor(
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
                {"p": "5810", "py": -23.5, "px": -46.6},
                {"p": "fora-selecao", "py": -23.4, "px": -46.5},
                {"p": "5811", "py": -23.3, "px": -46.4},
                {"p": "5810", "py": None, "px": -46.2},
            ],
        }
        cliente = SimpleNamespace(buscar_posicoes=Mock(return_value=resposta))
        modelo_selecao = SimpleNamespace(
            query=SimpleNamespace(all=Mock(return_value=selecionados))
        )

        with (
            patch.object(gps_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(gps_collector, "SPTransClient", return_value=cliente),
        ):
            total = gps_collector.coletar_ciclo()

        self.assertEqual(total, 2)
        cliente.buscar_posicoes.assert_called_once_with(123)
        self.session.add_all.assert_called_once()
        self.session.commit.assert_called_once_with()
        execucoes = [obj for obj in self.session.added if isinstance(obj, ExecucaoColeta)]
        self.assertEqual(len(execucoes), 1)
        self.assertEqual(execucoes[0].status, "concluida")
        self.assertEqual(execucoes[0].quantidade_registros, 2)
        registros = self.session.add_all.call_args.args[0]
        self.assertEqual(
            [(r.prefixo_onibus, r.codigo_linha, r.latitude, r.longitude) for r in registros],
            [("5810", 123, -23.5, -46.6), ("5811", 123, -23.3, -46.4)],
        )
        self.assertTrue(all(r.registrado_em.tzinfo is not None for r in registros))
        self.assertTrue(all(r.execucao_id == 77 for r in registros))

    def test_ciclo_sem_onibus_contabiliza_execucao_sem_consultar_api(self):
        modelo_selecao = SimpleNamespace(query=SimpleNamespace(all=Mock(return_value=[])))
        with (
            patch.object(gps_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(gps_collector, "SPTransClient") as cliente,
        ):
            total = gps_collector.coletar_ciclo()

        self.assertEqual(total, 0)
        cliente.assert_not_called()
        self.session.add_all.assert_not_called()
        self.session.commit.assert_called_once_with()
        self.assertEqual(self.session.added[0].status, "concluida")

    def test_cada_execucao_remove_sua_sessao_sql(self):
        relogio = RelogioFalso()
        gps_collector.executar_coletor(
            15,
            coletar=lambda: setattr(relogio, "agora", relogio.agora + 0.1),
            relogio=relogio.monotonic,
            dormir=relogio.dormir,
            max_ciclos=3,
        )
        self.assertEqual(self.session.remove.call_count, 3)

    def test_criar_app_flask_nao_inicia_coletor(self):
        with patch.object(gps_collector, "executar_coletor") as executar:
            app = create_app()
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
        with patch.object(gps_collector, "SPTransClient", return_value=cliente):
            with redirect_stdout(saida):
                gps_collector.imprimir_localizacao_atual("5810")
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
        with patch.object(gps_collector, "SPTransClient", return_value=cliente):
            with redirect_stdout(saida):
                gps_collector.imprimir_posicoes_da_linha("5810")
        cliente.buscar_posicoes.assert_called_once_with(123)
        self.assertIn("Prefixo: 9999", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
