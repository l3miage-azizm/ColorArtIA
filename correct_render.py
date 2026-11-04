"""Corriger le rendu colorié d'un dossier existant sans régénérer son masque."""
from pathlib import Path
import argparse
from core.final_render import correct_render_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('approved_run', type=Path, help='Dossier run contenant le masque validé et metadata.json')
    parser.add_argument('--output', type=Path, default=Path('outputs'), help='Dossier des nouvelles versions')
    args = parser.parse_args()
    try:
        paths = correct_render_run(args.approved_run, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(2, f'ColorArt : {exc}\n')
    print(f'Masque conservé : {paths["mystery"]}')
    print(f'Rendu colorié : {paths["solution_final"]}')
    print(f'Rapport : {paths["metadata"]}')


if __name__ == '__main__':
    main()
