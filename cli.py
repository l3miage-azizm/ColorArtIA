"""Standalone generation; the web studio adds the persistent book workflow."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from core.engine import GenerationConfig, generate_project
from core.universes import UNIVERSES


def main(argv=None):
    parser = argparse.ArgumentParser(description="ColorArt — photo vers coloriage adulte vérifiable")
    parser.add_argument("image", type=Path, help="Photo JPEG, PNG ou WebP")
    parser.add_argument("--output", type=Path, default=Path("outputs"), help="Dossier des rendus")
    parser.add_argument("--colors", type=int, default=24, help="Nombre maximal de couleurs, entre 2 et 48")
    parser.add_argument("--segments", type=int, default=900, help="Budget cible de régions, selon la difficulté")
    parser.add_argument("--min-area", type=int, default=180, help="Surface minimale à la résolution de travail")
    parser.add_argument("--universe", choices=list(UNIVERSES), default="original")
    parser.add_argument("--difficulty", choices=["relaxed", "balanced", "expert"], default="balanced")
    parser.add_argument("--fit", choices=["contain", "cover"], default="contain",
                        help="contain conserve toute la photo ; cover recadre les bords")
    parser.add_argument("--width", type=int, default=2480, help="Largeur finale ; 2480 pour A4 à 300 ppp")
    parser.add_argument("--height", type=int, default=3508, help="Hauteur finale")
    parser.add_argument("--seed", type=int, default=42, help="Graine reproductible de la palette")
    parser.add_argument("--density", choices=["natural", "rich"], default="rich")
    parser.add_argument("--regions", type=int, default=3200, help="Cible de zones (100 à 6000), adaptée à la lisibilité")
    parser.add_argument("--masking", choices=["guided", "discreet", "mystery"], default="mystery",
                        help="Effet de surprise : dessin visible, surprise douce ou mystère renforcé")
    parser.add_argument("--illustrated-input", action="store_true", help="Entrée déjà dessinée : conserver ses traits fins préimprimés")
    args = parser.parse_args(argv)
    try:
        config = GenerationConfig(n_colors=args.colors, n_segments=args.segments,
            min_region_area=args.min_area, fit_mode=args.fit, difficulty=args.difficulty,
            target_width=args.width, target_height=args.height, seed=args.seed,
            density=args.density, target_regions=args.regions, masking=args.masking,
            preserve_ink=args.illustrated_input)
        result = generate_project(args.image, args.output, config, args.universe)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"ColorArt : {exc}\n")
    print(f"Projet enregistré dans {result['run_dir']}")
    print(f"Page à colorier : {result['mystery']}")
    print(f"Résultat colorié : {result['solution_final']}")
    print(f"Contrôle à consulter avant impression : {result['quality']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
