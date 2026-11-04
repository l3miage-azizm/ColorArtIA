# ColorArt Atelier sur Vercel

Application : https://colorart-atelier.vercel.app
Projet : prj_6W6IHtAlvXpg6nUm6c9EWD7QY1T6
Équipe : team_MFQ1jVii28wiBRT1TqFf5eql
Stockage privé, région Paris : store_W0cemGhrG1obBuIV

Le moteur core/ et les fichiers HTML/CSS restent identiques à la base stable.
La fonction Python api/index.py rend les dessins dans la requête et publie les
sorties avant de confirmer le succès. Chaque requête utilise un dossier temporaire
propre. Les deux bases SQLite sont sauvegardées avec backup(), puis publiées avec
les médias immuables dans Blob privé. Un document par propriétaire bascule les
fichiers et les deux bases ensemble via un ETag conditionnel ; une modification
concurrente reçoit 409 et ne peut pas écraser une autre sauvegarde.

api/storage.mjs utilise le SDK officiel @vercel/blob et OIDC. Le secret de liaison
entre les deux fonctions reste dans les variables chiffrées Vercel. Aucun jeton
global de stockage ne va au navigateur. Les transferts directs utilisent une URL
à portée limitée au fichier, au type et à la taille, valide cinq minutes. Les PDF
et images privés sont délivrés après vérification du propriétaire.

Les variables nécessaires sont BLOB_STORE_ID et COLORART_STORAGE_BRIDGE_SECRET.
Le service artistique premium nécessite encore COLORART_IMAGE_API_KEY ou
OPENAI_API_KEY. Sans cette configuration, l’interface signale le mode local.
Aucun paiement ni imprimeur n’est connecté.

Limites actuelles : accès aux livres lié au cookie du navigateur, sans compte
récupérable ; rendus attendus dans la requête avec plafond Vercel ; conflit 409
possible entre deux éditions simultanées ; pas encore de nettoyage automatique
des anciens objets immuables et transferts abandonnés. La capacité maximale de
40 photos et le cas premium ne sont pas certifiés par le test unitaire de mise
en ligne. Un lancement commercial demande ces validations complémentaires.

Validation : tests/test_cloud_state.py, tests/test_cloud_http.py, puis
tools/live_smoke.py sur l’hébergement réel. Le rapport live-smoke-report.json
donne le résultat exact et doit être consulté avant de déclarer le parcours
complet opérationnel. Aucun test ne passe de commande réelle.
