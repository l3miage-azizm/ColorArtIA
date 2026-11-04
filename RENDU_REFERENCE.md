# ColorArt — rendu de référence et univers artistiques

Cette version rétablit le parcours illustration → découpage → masque → reconstruction. Les dessins de référence des cinq essais sont des illustrations créées auparavant avec ChatGPT, et non la preuve d’un générateur API actif. Les calculs de reconstruction des cinq dessins sauvegardés passent avec tous les codes disponibles sur la page ou dans les guides de détails.

## Ce qui change

- Protection des formes des yeux, de la bouche et des lunettes, avec détection locale YuNet ; secours géométrique explicite pour les sujets sans visage humain détecté.
- Réglage initial : 32 couleurs, détail expert, environ 3200 zones et masquage mystère. Les réglages des livres existants restent consultables.
- Guides de codes des microzones séparés du livre ; contrôles d’intégrité à la validation et à l’export.
- 18 univers artistiques, dont 15 nouveaux, séparés des palettes de couleurs. Une nouvelle scène nécessite une nouvelle illustration et son propre découpage.
- Réutilisation de l’illustration sauvegardée lors des changements de réglages ; aucune nouvelle génération payante pour un simple changement de couleurs.
- Cache d’affichage et protection contre les réponses obsolètes après des clics rapides.

## Mesures

Sur la même illustration, optimisation locale mesurée de 32,26 à 22,87 secondes (−29,1 %), à sorties strictement identiques pour cette optimisation. Avec la restauration des détails, les cinq références complètes prennent 22,6 à 32,5 secondes localement. Ces mesures excluent génération artistique distante, démarrage serveur et stockage cloud. Elles ne promettent pas ces mêmes délais sur Vercel.

Le comparatif est disponible dans `web/comparatif_reference.pdf`. La fidélité esthétique reste à vérifier sur chaque nouvelle illustration. Les tests de transport simulés ne démontrent ni la ressemblance d’un visage ni la qualité du fournisseur réel.

## État du service de dessin

Le 2 novembre 2026, la connexion OIDC réelle depuis Vercel vers AI Gateway est fonctionnelle, mais le fournisseur répond `insufficient_funds`. Aucun appel payant de création n’a été relancé. Les nouvelles illustrations et scènes restent indisponibles tant que les crédits ne sont pas activés. Le mode `COLORART_ARTISTIC_READY=auto` vérifie la disponibilité du fournisseur sans générer d’image et peut activer les univers après ajout de crédits et actualisation de la page. Les clés ne sont jamais envoyées au navigateur.

Serveur local : installer `requirements.lock`, puis lancer `python atelier.py`. Le fichier `app.py` sert uniquement au déploiement Vercel. L’impression et le paiement ne sont pas raccordés.
