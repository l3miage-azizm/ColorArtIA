"""Fifteen colour treatments preserve geometry; they do not invent scenery."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict
import cv2
import numpy as np

@dataclass(frozen=True)
class Universe:
    key: str
    label: str
    description: str
    saturation: float = 1.0
    contrast: float = 1.0
    brightness: float = 0.0
    temperature: float = 0.0
    tint: float = 0.0

UNIVERSES: Dict[str, Universe] = {
    "original": Universe("original", "Fidèle à la photo", "Couleurs et composition de votre photographie."),
    "royal": Universe("royal", "Héritage royal", "Ombres profondes et lumière légèrement dorée.", .95, 1.08, 1, 6, -1),
    "floral": Universe("floral", "Romance florale", "Tons tendres et lumineux, sans ajout de décor.", .86, .98, 5, 3, 3),
    "noel": Universe("noel", "Hiver de Noël", "Atmosphère fraîche aux contrastes nets.", .98, 1.08, -1, -6, 1),
    "sepia": Universe("sepia", "Souvenirs sépia", "Un souvenir aux nuances brunes et chaleureuses.", .0, 1.02, 2, 19, 2),
    "monochrome": Universe("monochrome", "Noir et blanc", "Une étude des ombres et de la lumière.", .0, 1.08),
    "pastel": Universe("pastel", "Douceur pastel", "Couleurs atténuées et lumière douce.", .66, .90, 9, 2),
    "golden": Universe("golden", "Heure dorée", "Chaleur d'une lumière de fin de journée.", 1.02, 1.04, 3, 10),
    "ocean": Universe("ocean", "Éclat océan", "Reflets frais, bleutés et légèrement désaturés.", .90, 1.03, 1, -9, -2),
    "forest": Universe("forest", "Écrin végétal", "Tonalité douce et légèrement verdoyante.", .92, 1.03, 0, -2, -7),
    "autumn": Universe("autumn", "Poésie d'automne", "Une palette chaude, cuivrée et enveloppante.", 1.06, 1.06, 1, 11, 1),
    "lavender": Universe("lavender", "Brume lavande", "Une touche froide et rosée sur des tons doux.", .78, .95, 5, -4, 8),
    "vintage": Universe("vintage", "Album ancien", "Contrastes assouplis et couleurs patinées.", .72, .91, 5, 6, 1),
    "cinema": Universe("cinema", "Portrait cinéma", "Des ombres plus denses et une couleur contenue.", .82, 1.15, -2, -2),
    "luminous": Universe("luminous", "Lumière de fête", "Un rendu lumineux aux couleurs légèrement renforcées.", 1.08, 1.01, 7, 3, 1),
}

def apply_universe(image_rgb: np.ndarray, universe_key: str) -> np.ndarray:
    if universe_key not in UNIVERSES:
        raise ValueError("L'univers sélectionné n'existe pas.")
    if universe_key == "original":
        return image_rgb.copy()
    u = UNIVERSES[universe_key]
    img = image_rgb.astype(np.float32) / 255.0
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * u.saturation, 0, 1)
    img = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB) * 255
    img = (img-127.5)*u.contrast + 127.5 + u.brightness
    img[:, :, 0] += u.temperature * .9 + u.tint * .4
    img[:, :, 1] -= u.tint * .4
    img[:, :, 2] -= u.temperature * .9
    return np.clip(np.rint(img), 0, 255).astype(np.uint8)
