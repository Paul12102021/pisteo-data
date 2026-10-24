# pisteo-data

Parcours balisés (GR, PR, véloroutes, EuroVelo, VTT) d'OpenStreetMap, préparés pour l'appli [Pisteo](https://paul12102021.github.io/balise-gpx/).

- Le 1er et le 15 de chaque mois, une GitHub Action télécharge l'extrait France de Geofabrik, ne garde que les relations `route=hiking|foot|bicycle|mtb`, simplifie les tracés et les découpe en tuiles de 0,25° (`build.py`).
- Les **points utiles** (eau potable, sources, toilettes, abris, refuges, cabanes, sommets, points de vue) sont préparés de la même façon (`poi.py`), pour la France et les pays voisins (Andorre, Belgique, Luxembourg, Suisse, Autriche, Espagne, Italie, Allemagne, Grande-Bretagne) : `poi.json` et `p/<x>_<y>.json`.
- Le résultat est publié sur la branche `gh-pages` (écrasée à chaque fois, sans historique) et servi par GitHub Pages : `manifest.json` et `t/<x>_<y>.json`.
- Lancer à la main : onglet **Actions** → **Parcours balisés** → **Run workflow**.

Données © contributeurs OpenStreetMap, sous licence [ODbL](https://www.openstreetmap.org/copyright).
