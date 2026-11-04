# Hébergement séparé : Netlify + Render

Le navigateur utilise une seule origine Netlify. Netlify sert l’interface et transmet `/api/*` au serveur Python Render ; les cookies de session restent sur l’origine de l’interface. Le serveur conserve SQLite, les photos et les exports sur un disque persistant. Les traitements de dessin utilisent la file locale : conserver une seule instance et un seul worker.

## 1. Serveur Python Render

Dans Render, créer un Blueprint depuis `l3miage-azizm/ColorArtIA`, branche `main`, avec `render.yaml`. **Le plan Standard et le disque persistant de 10 Go sont payants** ; vérifier le tarif affiché avant de créer le service. Le plan gratuit et son stockage éphémère ne conviennent pas aux données des livres.

Le Blueprint installe Python 3.12 et les dépendances verrouillées, lance la factory ASGI `atelier:create_app` et configure la vérification `/api/health`. Le disque est monté sur `/var/data`, les données applicatives sont dans `/var/data/colorart`. Attendre que la vérification de santé réussisse, puis relever l’origine HTTPS attribuée par Render.

La génération IA reste optionnelle : configurer `COLORART_IMAGE_API_KEY` dans les secrets Render si nécessaire ; ne jamais la mettre dans Git ou dans les variables du navigateur. L’impression et le paiement ne sont pas intégrés.

## 2. Interface Netlify

Importer ce dépôt dans Netlify, branche `main`. `netlify.toml` fournit la commande de construction et le dossier `dist`. Ajouter la variable **non secrète** `COLORART_BACKEND_URL` avec l’origine HTTPS du service Render, sans chemin. Puis déclencher le déploiement. Si l’URL manque, la construction publie une page temporaire clairement signalée, sans création de livres ni enregistrement de photos. Configurer `COLORART_BACKEND_URL` puis redéployer active automatiquement l’interface complète et le proxy API. Une URL configurée mais invalide fait toujours échouer la construction.

Pour vérifier la construction localement :

```sh
COLORART_BACKEND_URL=https://votre-service.onrender.com node deployment/build-netlify.mjs
```

Le dossier `dist` est généré et ignoré par Git. Les fichiers JS/CSS sont disponibles sous `/static/`, comme dans le serveur local. Les requêtes `/api/*` sont transmises au serveur Python par une règle proxy forcée ; aucun secret n’est inclus dans la construction.

## 3. Validation après déploiement

Depuis le domaine Netlify : vérifier `/api/health` (`status: ok`), le catalogue et la création d’un livre. Ajouter une photo, effectuer un rendu local, rouvrir le livre et télécharger un export. Redémarrer ensuite le service Render et vérifier que le livre existe encore. Tester séparément un rendu IA réel si sa clé a été configurée. Ces validations distantes restent à exécuter une fois les services créés.

Les transferts et exports restent soumis aux limites de proxy Netlify. Tester notamment les photos et livres volumineux avant une mise en production. Le cookie du navigateur constitue l’accès actuel aux livres : il n’existe pas de compte récupérable. Prévoir des sauvegardes régulières du disque ; ce Blueprint ne configure pas de sauvegarde externe.

Aucun service Render ou Netlify n’est créé par l’ajout de ces fichiers au dépôt.
