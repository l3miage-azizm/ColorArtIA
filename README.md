# ColorArt Atelier — version artistique

Application web de livres de coloriage personnalisés, avec comparaison photo/dessin, favoris par photo, organisation des pages et préparation de PDF.

## Rendu

Le parcours premium crée d’abord une vraie illustration, puis prépare son coloriage masqué. Le moteur protège les yeux, les lunettes et la bouche ; les microzones disposent de codes dans des guides séparés. Le masquage conserve le style des cinq essais validés.

Les 18 univers artistiques (dont 15 nouveaux) changent la scène dessinée. Les palettes sont proposées dans un onglet distinct. Une scène déjà enregistrée peut être recolorée ou retravaillée sans nouvel appel au générateur.

## Lancement local

Python 3.12 est requis. Depuis ce dossier :

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python atelier.py
```

Ouvrir l’adresse indiquée dans le terminal. `app.py` est le point d’entrée Vercel, pas la commande de lancement local. Les fichiers locaux sont placés dans `data/` par défaut. Une connexion au fournisseur de dessin est nécessaire pour créer de nouvelles illustrations. Les clés doivent rester dans les variables du serveur.

## Vercel

La configuration utilise FastAPI, un pont Node vers un stockage Blob privé et l’identité OIDC renouvelée à chaque requête. Voir `VERCEL.md` pour le stockage et `RENDU_REFERENCE.md` pour l’état du rendu.

Pour les illustrations via AI Gateway : `COLORART_IMAGE_PROVIDER=vercel_gateway` et `COLORART_ARTISTIC_READY=auto`. Ce mode vérifie la disponibilité sans créer d’image ; l’atelier se débloque après activation des crédits et actualisation de la page. Aucun jeton OIDC ni clé API n’est envoyé au navigateur.

Vérification réelle du 2 novembre 2026 : connexion au fournisseur réussie, création bloquée par `insufficient_funds`. Aucun test simulé ne remplace une validation visuelle réelle du générateur sur de nouvelles photos. L’impression et le paiement restent à raccorder.

## Vérification

```sh
python -m pytest tests/ -q
```

Les tests couvrent l’intégrité des fichiers, la séparation des ateliers, les scènes et palettes, la réutilisation des illustrations, les microzones et les exports. Les tests utilisant un fournisseur simulé ne prouvent pas la ressemblance des visages. Les cinq illustrations de référence ont été retravaillées avec reconstruction exacte depuis les zones sauvegardées.
