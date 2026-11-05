# ColorArtIA — essai Render Free + Cloudflare Workers AI

Cette configuration sert **uniquement à tester** ColorArtIA sans abonnement payant. Le déploiement Vercel et le déploiement Render Standard existants ne sont pas modifiés.

## 1. Cloudflare

1. Ouvrir https://dash.cloudflare.com/ puis **Workers AI → Use REST API**.
2. Créer un **Workers AI API Token** avec le modèle de permissions recommandé par Cloudflare, et copier **Account ID**.
3. Conserver le token **uniquement côté serveur** (variables privées Render). Ne jamais le committer, l'envoyer au navigateur ou le copier dans une issue GitHub.
4. Rester sur l'offre Workers Free. Au-delà de l'allocation quotidienne, le fournisseur refuse les requêtes, sans bascule automatique vers OpenAI.

Docs : https://developers.cloudflare.com/workers-ai/get-started/rest-api/
Modèle : https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/
Quota : https://developers.cloudflare.com/workers-ai/platform/pricing/

## 2. Render : créer **manuellement** un Web Service gratuit

**Ne pas créer un Blueprint depuis le `render.yaml` existant : il spécifie `plan: standard` et un disque payant.**

- Repository : `l3miage-azizm/ColorArtIA`
- Branche de test : `feature/cloudflare-workers-ai-klein` (après fusion validée, utiliser `main`).
- Language : **Python 3** ; région : **Frankfurt** ; instance : **Free**.
- Root Directory : vide.
- Build Command : `python -m pip install -r requirements.lock`
- Start Command : `python -m uvicorn atelier:create_app --factory --host 0.0.0.0 --port "$PORT" --workers 1 --proxy-headers --forwarded-allow-ips '*'`
- Health check path : `/api/health`.

**Environment variables :**

| Key | Value |
| --- | --- |
| `PYTHON_VERSION` | `3.12.14` |
| `OMP_NUM_THREADS` | `2` |
| `OPENBLAS_NUM_THREADS` | `2` |
| `COLORART_IMAGE_PROVIDER` | `cloudflare` |
| `COLORART_IMAGE_MODEL` | `@cf/black-forest-labs/flux-2-klein-4b` |
| `CLOUDFLARE_ACCOUNT_ID` | identifiant privé du compte |
| `CLOUDFLARE_API_TOKEN` | token privé Workers AI |
| `COLORART_ARTISTIC_READY` | `1` |

Supprimer/laisser **non définies** `COLORART_IMAGE_BASE_URL` et `COLORART_DATA_DIR`. Ne pas utiliser une variable `VERCEL` ou une clé AI Gateway pour ce fournisseur.

La génération locale reste accessible, les providers `openai` et `vercel_gateway` restent utilisables lorsqu'ils sont sélectionnés.

## 3. Vérifier sans dépenser de crédits

- La route `/api/health` doit renvoyer `{"status":"ok", ...}`.
- La route `/api/catalog` doit contenir les univers artistiques disponibles si le token est présent et la configuration valide. **Cela ne prouve PAS que le token autorise une génération**.
- Depuis une machine de développement avec dépendances et pytest, exécuter `python -m pytest tests/test_cloudflare_artist.py -q`, puis `python -m pytest tests/ -q`.
- Les tests utilisent une réponse Cloudflare simulée et **n'envoient aucune requête facturée**.
- Avec un token réel, réaliser un premier essai d'édition sur **une photo de test non sensible**. Vérifier la fidélité des visages, les contours, puis le coloriage et le PDF. Un rendu humainement validé reste nécessaire.

## 4. Limites connues

- **Stockage non persistant** : Render Free efface images, bases SQLite et PDF lors d'une mise en veille, d'un redémarrage ou d'un redéploiement. Ne pas conserver d'images clients dans ce service.
- **Mémoire** : le moteur de segmentation, NumPy, SciPy et OpenCV peuvent dépasser les ressources du Free, même si l'édition distante aboutit. Tester une page puis augmenter progressivement.
- **Référence Cloudflare** : le modèle exige une image d'entrée strictement inférieure à 512×512. L'intégration réduit la photo à **496 px maximum**, au détriment potentiel de la ressemblance et des détails.
- **Sortie IA** : demande 1024×1536, pas la définition A4 finale. La qualité finale d'impression n'est pas garantie.
- **Quotas** : Workers AI Free inclut 10 000 neurones/jour (partagés avec les autres usages du compte). Consulter les métriques Cloudflare. Les appels échouent lorsque l'allocation est épuisée ; aucune image de substitution n'est produite.
- **Coût et sécurité** : l'endpoint d'édition est accessible via l'application publique. Avant une vraie mise en production, ajouter authentification, limite de générations par utilisateur et stockage pérenne. Ne pas activer d'offre payante Cloudflare sans une politique de budget.

Ces limites sont incompatibles avec un lancement commercial fiable ; ce déploiement est une preuve de concept.
