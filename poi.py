#!/usr/bin/env python3
"""Points utiles pour la randonnée (eau, sources, toilettes, abris, refuges) pour Pisteo.

Entrée : un extrait .osm.pbf déjà filtré (osmium tags-filter … voir build.yml).
Sortie : des tuiles JSON de 0,25° × 0,25° (p/<x>_<y>.json) et poi.json (liste des tuiles).

Chaque tuile : {"p": [[lat, lon, type, nom?], …]} avec lat/lon arrondis à 1e-5 et type :
  w eau potable · s source · t toilettes · a abri · r refuge gardé · c cabane non gardée
  p sommet (avec son altitude en 5e valeur) · v point de vue
  e épicerie, supérette, supermarché, primeur, magasin de producteurs · b boulangerie
    (commerces : horaires OSM « opening_hours » en 5e valeur quand ils sont connus)
Données © contributeurs OpenStreetMap, licence ODbL.
"""
import json, math, os, sys, time
from collections import defaultdict

import osmium

GRID = 0.25


def tile_of(lat, lon):
    return int(math.floor((lon + 180) / GRID)), int(math.floor((lat + 90) / GRID))


def kind(t):
    """Type Pisteo d'un objet OSM, ou None s'il ne nous intéresse pas."""
    a, n, tour, mm = t.get('amenity'), t.get('natural'), t.get('tourism'), t.get('man_made')
    if t.get('access') in ('private', 'no') or 'disused:amenity' in t:
        return None
    if a == 'drinking_water' or (mm == 'water_tap' and t.get('drinking_water') == 'yes') \
            or (a == 'fountain' and t.get('drinking_water') == 'yes'):
        return 'w'
    if n == 'spring':
        return 'w' if t.get('drinking_water') == 'yes' else 's'
    if tour == 'alpine_hut':
        return 'r'
    if tour == 'wilderness_hut':
        return 'c'
    if a == 'shelter' and t.get('shelter_type') not in ('public_transport', 'bus_stop', 'parking', 'field_shelter', 'sun_shelter'):
        return 'a'
    if a == 'toilets':
        return 't'
    if n == 'peak' and (t.get('name') or t.get('ele')):
        return 'p'
    if tour == 'viewpoint':
        return 'v'
    sh = t.get('shop')
    if sh in ('supermarket', 'convenience', 'general', 'greengrocer', 'deli', 'farm'):
        return 'e'
    if sh in ('bakery', 'pastry'):
        return 'b'
    return None


class H(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.out = []

    def add(self, t, lat, lon):
        k = kind(t)
        if not k:
            return
        name = t.get('name:fr') or t.get('name') or ''
        ele = None
        if k == 'p':
            try:
                ele = round(float(str(t.get('ele', '')).replace(',', '.').split(';')[0].replace('m', '').strip()))
            except ValueError:
                ele = None
        if k in ('e', 'b'):
            ele = (t.get('opening_hours') or '')[:160] or None  # horaires du commerce (texte OSM)
        self.out.append((round(lat, 5), round(lon, 5), k, name[:60], ele))

    def node(self, n):
        if n.location.valid():
            self.add(n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        # bâtiment (refuge, abri…) dessiné en surface : son centre
        la = lo = 0.0
        c = 0
        for nd in w.nodes:
            if nd.location.valid():
                la += nd.location.lat
                lo += nd.location.lon
                c += 1
        if c:
            self.add(w.tags, la / c, lo / c)


def main(src, out):
    t0 = time.time()
    h = H()
    h.apply_file(src, locations=True)
    tiles = defaultdict(list)
    count = defaultdict(int)
    for lat, lon, k, name, ele in h.out:
        row = [lat, lon, k, name] if name or ele is not None else [lat, lon, k]
        if ele is not None:
            row.append(ele)
        tiles[tile_of(lat, lon)].append(row)
        count[k] += 1
    os.makedirs(os.path.join(out, 'p'), exist_ok=True)
    names, total = [], 0
    for (x, y), pts in tiles.items():
        data = json.dumps({'p': pts}, ensure_ascii=False, separators=(',', ':'))
        name = f'{x}_{y}'
        with open(os.path.join(out, 'p', name + '.json'), 'w', encoding='utf-8') as f:
            f.write(data)
        names.append(name)
        total += len(data)
    names.sort()
    with open(os.path.join(out, 'poi.json'), 'w', encoding='utf-8') as f:
        json.dump({'v': 1, 'date': time.strftime('%Y-%m-%d'), 'grid': GRID, 'tiles': names, 'count': count, 'bytes': total},
                  f, ensure_ascii=False, separators=(',', ':'))
    print(f'points utiles : {len(h.out)} ({dict(count)}), {len(names)} tuiles, {total / 1e6:.1f} Mo, {time.time() - t0:.0f} s', flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
