# file-transfer — transfert de fichiers chiffrés entre utilisateurs

Chaque utilisateur envoie des fichiers (chiffrés AES-256-GCM sur le disque du serveur), les partage avec d'autres
utilisateurs, et télécharge ceux qu'on a partagés avec lui. Même squelette que `game-hub` (Flask + gunicorn + Docker,
auth via `auth_client`, base PostgreSQL distante).

## Lancer en local (sans authentification)
```bash
pip install -r requirements.txt      # sans auth-client
cp .env.example .env                 # renseigner ENCRYPTION_KEY et DB_* ; AUTH_ENABLED=false, URL_PREFIX= (vide)
flask --app wsgi run --debug --port 5000
```
Accueil : `http://localhost:5000/` (ou `http://localhost:5000<URL_PREFIX>/` si un préfixe est défini).
Sans auth, tout le monde est l'utilisateur `DEV_USERNAME` ; pour tester un partage, relancer avec un autre `DEV_USER_ID`
/ `DEV_USERNAME` (les utilisateurs déjà passés par la page apparaissent dans « Partager avec »).

## Production (avec authentification)
```bash
pip install -r requirements-auth.txt   # ajoute auth-client
# .env : AUTH_ENABLED=true + variables AUTH_* + URL_PREFIX=/file-transfer
```

## Structure
```
wsgi.py                  point d'entrée (gunicorn / flask)
app/
  __init__.py            create_app : préfixe d'URL, proxy, vérification de la clé, base de données
  config.py              lecture du .env
  helpers.py             require_auth (auth_client si AUTH_ENABLED) + filtre « taille lisible »
  crypto.py              chiffrement/déchiffrement AES-256-GCM (seul fichier à adapter pour changer d'algorithme)
  db.py                  PostgreSQL : pool de connexions + utilisateurs, fichiers, partages (seul fichier à adapter pour une autre base)
  schema.sql             création des tables ft_* (lu par db.py, utilisable à la main avec psql)
  users.py               utilisateur courant (g.user) + annuaire Keycloak pour « Partager avec »
  fileservice.py         logique métier : upload / download / delete (droits + chiffrement + disque + base)
  routes.py              blueprint « files » : page, /upload, /download/<id>, /share, DELETE /<id>
  templates/             base.html, files.html, partials/ (_my_files, _shared_files, _icons)
  static/                css/site.css (variables), css/files.css, js/files.js
```

## Routes
- `<URL_PREFIX>/` : mes fichiers + fichiers partagés avec moi
- `<URL_PREFIX>/upload` : POST multipart (`file`)
- `<URL_PREFIX>/download/<id>` : téléchargement (propriétaire ou utilisateur avec qui le fichier est partagé)
- `<URL_PREFIX>/share` : POST JSON `{file_id, user_id, add}` (propriétaire uniquement)
- `<URL_PREFIX>/<id>` : DELETE (propriétaire uniquement)

Toutes les routes exigent `@require_auth()`. Pour restreindre à un rôle Keycloak : `@require_auth("mon-role")`
dans `routes.py`.

## Chiffrement
- Clé : `ENCRYPTION_KEY` (obligatoire, 32 octets en base64, commande de génération dans `.env.example`).
  L'application refuse de démarrer sans clé valide.
- **Perdre ou changer la clé rend tous les fichiers existants illisibles.** La sauvegarder séparément du serveur.
- Le fichier est chiffré/déchiffré **en mémoire** : `MAX_UPLOAD_MB` (100 par défaut) limite la taille, et la RAM
  nécessaire est de l'ordre de la taille du fichier × nombre de workers actifs.
- Sur le disque, les fichiers sont nommés `<uuid>.enc` (sans l'extension d'origine). Le nom d'origine n'est qu'en base.

## Données
Tables dans la base PostgreSQL distante (préfixe `ft_` : aucun conflit si la base est partagée avec `game-hub`) :
`ft_users`, `ft_files`, `ft_file_shares`. Elles doivent exister : `DB_AUTO_CREATE=true` les crée au démarrage
(droit CREATE requis), ou bien lancer une fois `psql -h <hote> -U <user> -d <base> -f app/schema.sql`.

Au démarrage, l'application vérifie la connexion et la présence des tables : en cas de problème (hôte injoignable,
mauvais mot de passe, tables absentes, droit CREATE manquant) elle s'arrête avec un message explicite, au lieu de
renvoyer une erreur 500 à la première requête. Les connexions viennent d'un pool (`DB_POOL_MIN` / `DB_POOL_MAX`, par
worker gunicorn) ; ne pas lancer gunicorn avec `--preload`. Base coupée en cours de route : les requêtes reçoivent un
503, puis tout repart seul au retour de la base.

Les utilisateurs sont ceux de Keycloak (`GET /api/users` du service d'auth) et sont recopiés dans `ft_users`.
Si le service d'auth est injoignable, le menu « Partager avec » se limite aux utilisateurs déjà connus en base.

## Docker
```bash
cp .env.example .env                 # renseigner PORT, SECRET_KEY, ENCRYPTION_KEY et DB_*
docker network create web            # une seule fois (réseau partagé avec le reverse proxy / l'auth)
docker compose up -d --build
```
En production (auth activée) : `WITH_AUTH=true docker compose up -d --build` avec `AUTH_ENABLED=true` dans le `.env`.

Les fichiers chiffrés sont dans le volume Docker `uploads` (monté sur `/data/uploads`). **Le sauvegarder, avec la clé.**

### Reverse proxy
Le reverse proxy doit laisser passer les gros fichiers, sinon l'upload échoue avant d'atteindre l'application.
Exemple nginx, dans le bloc `location /file-transfer/` :
```nginx
client_max_body_size 110m;      # un peu plus que MAX_UPLOAD_MB
proxy_read_timeout   120s;
```
Derrière un proxy HTTPS, mettre `BEHIND_PROXY=true` et `SESSION_COOKIE_SECURE=true`.
