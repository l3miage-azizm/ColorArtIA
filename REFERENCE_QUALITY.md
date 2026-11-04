# Rendu de référence et restauration

La photo jointe par l’utilisateur le 2 novembre 2026 est la planche « ColorArt — cinq nouveaux essais ». Les cinq exemples complets sont conservés dans `colorart_pro/dist/cinq-tests-20261031`. Aucun de ces fichiers n’a été remplacé.

## Origine du rendu apprécié

Le fichier `verification.json` de cette livraison précise que les photographies fictives et les dessins de référence ont été créés séparément avec le générateur d’images ChatGPT. Le découpage, la page masquée et le coloriage ont ensuite été calculés par le code expérimental `colorart_render_lab/experiments/five_test_engine.py`.

Cette planche ne prouve donc pas que le service artistique de l’application était connecté. Le découpage direct d’une photographie, même avec davantage de zones, ne produit pas le trait volontaire et les aplats de l’illustrateur. La génération photo → dessin et la reconstruction dessin → coloriage doivent être vérifiées séparément.

## Écarts du moteur standard

L’application utilisait par défaut 24 couleurs et le niveau équilibré. La planche appréciée utilisait 32 couleurs, le niveau expert, un budget initial de 2 000 composantes, une aire minimale de 85 à la résolution de travail, la conservation des traits et le masquage mystère. Son masquage employait une encre grise 190, des traits affinés à 2 pixels pour une page A4 de 2 480 pixels de large, et des codes gris 72.

Le moteur standard fusionnait les formes trop étroites pour recevoir un code jusqu’à obtenir une couverture complète sur la page principale. Cette opération pouvait supprimer une pupille, le bord des lunettes ou une nuance de la bouche. La subdivision ultérieure en 3 200 zones ne rétablit aucun détail : elle divise une même couleur en plusieurs régions.

Le moteur expérimental de la planche conservait les formes naturelles importantes des yeux, de la bouche et des lunettes à partir d’annotations manuelles. Les codes impossibles à imprimer sur la page principale figuraient dans des guides agrandis : 46 à 118 codes selon l’exemple, avec 3 à 8 pages de guides.

## Règles à conserver

- Le dessin amont doit rester un dessin identifiable, avec des contours propres, des aplats et des visages fidèles. Une recoloration n’est pas un univers artistique.
- Le style de masquage apprécié reste la référence. Un univers qui modifie la scène nécessite sa propre carte de zones ; une palette sur une même scène réutilise cette carte.
- Les détails ne doivent pas être fusionnés uniquement pour améliorer le taux de codes sur la page principale.
- Chaque guide agrandit la vraie carte avec un échantillonnage au plus proche. Chaque pixel du code doit rester dans la région à laquelle sa couleur correspond.
- La solution coloriée doit être reconstructible exactement depuis la carte, le nuancier et la même encre préimprimée. Aucun contour noir absent du support à colorier ne doit apparaître secrètement dans la solution.
- Une zone sans code, y compris après guides, doit rester explicitement signalée et empêcher une validation trompeuse.

Les tests numériques vérifient ces propriétés. Ils ne certifient pas une identité, une impression physique ou le confort de coloriage d’une microzone. Le contrôle visuel des cinq références et une épreuve imprimée restent nécessaires.

## Vérification reproductible

`tools/reference_regression.py` reprend les cinq vrais dessins de cette planche et génère les pages avec le moteur courant. Il contrôle les empreintes des sources, la reconstruction NPZ, le placement réel des codes et les pertes de détails dans les zones de contrôle déjà annotées. Il enregistre un rapport et un comparatif comprenant la photo, le dessin de référence, le coloriage antérieur, le nouveau masque et la nouvelle reconstruction.

Cette commande ne lance pas le fournisseur artistique et ne doit pas être présentée comme un test réel du service photo → dessin :

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python tools/reference_regression.py \
  --fixtures /workspace/colorart_pro/dist/cinq-tests-20261031 \
  --output artifacts/reference-regression
```

La campagne exécutée sur ces cinq dessins donne les résultats suivants. Les durées mesurent le calcul local du découpage, du masque, de la solution et des guides ; elles excluent la génération artistique distante et le stockage web.

| Exemple | Calcul local | Zones | Codes dans les guides | Pages de guides |
| --- | ---: | ---: | ---: | ---: |
| Femme à lunettes | 22,648 s | 3 200 | 49 | 4 |
| Homme à barbe | 32,538 s | 3 200 | 105 | 7 |
| Couple | 29,784 s | 3 200 | 64 | 4 |
| Grand-père et enfant | 31,469 s | 3 200 | 132 | 9 |
| Chien tricolore | 31,318 s | 3 123 | 131 | 9 |

Les cinq résultats sont exactement reconstructibles depuis leur NPZ ; chaque numéro de la page reste dans sa zone et tous les codes sont présents sur la page principale ou dans les guides. Les empreintes des dessins fournis restent inchangées. Le contrôle visuel des comparatifs confirme un rendu très proche du coloriage antérieur apprécié.

Le diagnostic de conservation des détails reste `needs_review` dans les cinq cas, comme pour les exemples antérieurs. Certaines mesures locales progressent et d’autres baissent légèrement. Cette restauration ne supprime donc pas toute simplification des yeux, des bouches ou des poils et ne constitue pas une validation commerciale automatique.
