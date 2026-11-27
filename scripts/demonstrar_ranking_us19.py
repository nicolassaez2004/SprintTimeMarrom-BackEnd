"""API HTTP com a amostra C20 sintética; nunca acessa o banco da equipe."""
import argparse
import json
from pathlib import Path

from werkzeug.serving import make_server

from scripts.validar_us19_t04 import ambiente_controlado, semear_ranking


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--porta', type=int, default=5095)
    parser.add_argument('--fixture', type=Path, help='Opcional: salvar a resposta JSON C20 da API')
    args = parser.parse_args()
    if not 1 <= args.porta <= 65535:
        parser.error('porta deve estar entre 1 e 65535')
    with ambiente_controlado() as app:
        semear_ranking(app)
        if args.fixture:
            r = app.test_client().get('/atrasos/ranking?mes=2026-11&linha=8000-10&sentido=0')
            assert r.status_code == 200
            args.fixture.write_text(json.dumps(r.json, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        with make_server('127.0.0.1', args.porta, app, threaded=True) as servidor:
            print(f'CONTROLE SINTETICO C20: http://127.0.0.1:{args.porta}/atrasos/ranking?mes=2026-11')
            print('Metrica MEDIA somente neste ensaio. Ctrl+C encerra e descarta o SQLite.')
            try:
                servidor.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == '__main__':
    main()
