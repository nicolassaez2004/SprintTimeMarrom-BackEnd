import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import gps_collector


class TestGpsCollector(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            add_all=Mock(),
            commit=Mock(),
            rollback=Mock(),
            remove=Mock(),
        )
        self.db_patcher = patch.object(
            gps_collector, "db", SimpleNamespace(session=self.session)
        )
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    def test_intervalo_configurado_e_30_segundos(self):
        self.assertEqual(gps_collector.INTERVALO_COLETA_SEGUNDOS, 30)

    def test_ciclo_consulta_cada_linha_uma_vez_e_grava_so_onibus_selecionado(self):
        selecionados = [
            SimpleNamespace(prefixo="5810", codigo_linha=123),
            SimpleNamespace(prefixo="5811", codigo_linha=123),
        ]
        resposta = {
            "hr": "12:00",
            "vs": [
                {"p": "5810", "py": -23.5, "px": -46.6},
                {"p": "outro", "py": -23.4, "px": -46.5},
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
        registros = self.session.add_all.call_args.args[0]
        self.assertEqual(
            [(r.prefixo_onibus, r.codigo_linha, r.latitude, r.longitude) for r in registros],
            [
                ("5810", 123, -23.5, -46.6),
                ("5811", 123, -23.3, -46.4),
            ],
        )
        self.assertTrue(all(r.registrado_em.tzinfo is not None for r in registros))

    def test_ciclo_sem_onibus_nao_consulta_api_nem_grava(self):
        modelo_selecao = SimpleNamespace(query=SimpleNamespace(all=Mock(return_value=[])))

        with (
            patch.object(gps_collector, "OnibusSelecionado", modelo_selecao),
            patch.object(gps_collector, "SPTransClient") as cliente,
        ):
            total = gps_collector.coletar_ciclo()

        self.assertEqual(total, 0)
        cliente.assert_not_called()
        self.session.add_all.assert_not_called()
        self.session.commit.assert_not_called()

    def test_imprime_localizacao_de_um_prefixo_sem_gravar(self):
        resposta = {
            "hr": "12:00",
            "l": [
                {
                    "c": "5810-10",
                    "cl": 123,
                    "vs": [{"p": 5810, "py": -23.5, "px": -46.6, "ta": "12:00:00Z"}],
                }
            ],
        }
        cliente = SimpleNamespace(buscar_todas_posicoes=Mock(return_value=resposta))
        saida = io.StringIO()
        with patch.object(gps_collector, "SPTransClient", return_value=cliente):
            with redirect_stdout(saida):
                gps_collector.imprimir_localizacao_atual("5810")

        self.assertIn("Prefixo: 5810", saida.getvalue())
        self.assertIn("Latitude: -23.5", saida.getvalue())
        self.assertIn("Longitude: -46.6", saida.getvalue())

    def test_imprime_posicoes_dos_veiculos_encontrados_pela_linha(self):
        cliente = SimpleNamespace(
            buscar_linhas=Mock(
                return_value=[
                    {"cl": 123, "lt": "5810", "sl": 1},
                    {"cl": 123, "lt": "5810", "sl": 2},
                ]
            ),
            buscar_posicoes=Mock(
                return_value={
                    "hr": "12:00",
                    "vs": [{"p": "9999", "py": -23.5, "px": -46.6, "ta": "12:00:00Z"}],
                }
            ),
        )
        saida = io.StringIO()
        with patch.object(gps_collector, "SPTransClient", return_value=cliente):
            with redirect_stdout(saida):
                gps_collector.imprimir_posicoes_da_linha("5810")

        cliente.buscar_linhas.assert_called_once_with("5810")
        cliente.buscar_posicoes.assert_called_once_with(123)
        self.assertIn("Prefixo: 9999", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
