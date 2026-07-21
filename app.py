# -*- coding: utf-8 -*-
"""
Recuperación Dewey — servicio intermedio.
Consulta por SRU los catálogos de bibliotecas nacionales que clasifican con
Dewey, extrae el campo MARC 082 (Clasificación Decimal Dewey) y lo devuelve
normalizado a la página. También consulta el archivo de autoridades de
nombres (NAF) de la Library of Congress para verificar autores.

Fuentes (fase 1):
  · Library of Congress — SRU público, sin registro (lx2.loc.gov:210/lcdb;
    la ruta https /sru/lcdb quedó fuera de servicio con la migración a Folio, jul. 2025)
  · Deutsche Nationalbibliothek — SRU público, datos CC0 (services.dnb.de/sru/dnb)
  · National Library of Scotland — preparada pero DESACTIVADA: su acceso
    confirmado es Z39.50 (Alma); el endpoint SRU queda pendiente de verificar.
"""
from flask import Flask, request, jsonify, send_file
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from pymarc import MARCReader
import xml.etree.ElementTree as ET
import requests, re, os

app = Flask(__name__)


@app.after_request
def _cors(respuesta):
    """La API es pública y de solo lectura: se permite consultarla desde otras
    páginas (en particular, Clasificación documental, que la usa como motor)."""
    respuesta.headers['Access-Control-Allow-Origin'] = '*'
    respuesta.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    return respuesta

CABECERAS = {'User-Agent': 'RecuperacionDewey/1.0 (herramienta bibliotecaria de codigo abierto)'}
TIEMPO_MAX = 9  # segundos por fuente

FUENTES = [
    {
        'clave': 'loc', 'nombre': 'Library of Congress', 'sigla': 'LoC', 'activa': True,
        'base': 'http://lx2.loc.gov:210/lcdb', 'esquema': 'marcxml',
        'idx': {'isbn': 'bath.isbn', 'titulo': 'dc.title', 'autor': 'dc.author'},
    },
    {
        'clave': 'dnb', 'nombre': 'Deutsche Nationalbibliothek', 'sigla': 'DNB', 'activa': True,
        'base': 'https://services.dnb.de/sru/dnb', 'esquema': 'MARC21-xml',
        'idx': {'isbn': 'dnb.num', 'titulo': 'dnb.tit', 'autor': 'dnb.atr'},
    },
    {
        'clave': 'bne', 'nombre': 'Biblioteca Nacional de España', 'sigla': 'BNE', 'activa': True,
        'base': 'https://catalogo.bne.es/view/sru/34BNE_INST', 'esquema': 'marcxml', 'version': '1.2',
        'idx': {'isbn': 'alma.isbn', 'titulo': 'alma.title', 'autor': 'alma.creator'},
    },
    {
        'clave': 'nls', 'nombre': 'National Library of Scotland', 'sigla': 'NLS', 'activa': False,
        'base': '', 'esquema': 'marcxml', 'idx': {},
        'nota': 'Acceso confirmado por Z39.50 (Alma); endpoint SRU pendiente de verificación.',
    },
]

NAF_BASE = 'http://lx2.loc.gov:210/naf'


# ---------------------------- utilidades MARC ----------------------------

def _local(tag):
    """Nombre del elemento sin el espacio de nombres XML."""
    return tag.split('}')[-1]


def _registros_marc(xml_texto):
    """Devuelve los elementos <record> MARC (los que tienen datafields)."""
    raiz = ET.fromstring(xml_texto)
    registros = []
    for el in raiz.iter():
        if _local(el.tag) == 'record' and any(_local(h.tag) == 'datafield' for h in el):
            registros.append(el)
    return registros


def _subcampos(datafield):
    subs = {}
    for sf in datafield:
        if _local(sf.tag) == 'subfield':
            codigo = sf.get('code') or ''
            subs.setdefault(codigo, []).append((sf.text or '').strip())
    return subs


def normalizar_ddc(valor):
    """'863/.64' → '863.64'; quita marcas de segmentación, espacios y puntuación final.
    Devuelve '' si no parece un número Dewey."""
    v = (valor or '').replace('/', '').strip()
    v = v.split()[0] if v.split() else ''
    v = v.rstrip('.').strip()
    return v if re.match(r'^\d{3}(\.\d+)?$', v) else ''


def _anio_de(texto):
    m = re.search(r'\d{4}', texto or '')
    return m.group(0) if m else ''


def parsear_bibliografico(registro):
    """De un registro MARC bibliográfico extrae Dewey (082), título, autor, año, editorial."""
    datos = {'ddc': [], 'udc': [], 'ddc_edicion': '', 'titulo': '', 'autor': '', 'anio': '', 'editorial': ''}
    for campo in registro:
        nombre = _local(campo.tag)
        if nombre == 'controlfield' and campo.get('tag') == '008':
            if not datos['anio']:
                datos['anio'] = _anio_de((campo.text or '')[7:11])
        if nombre != 'datafield':
            continue
        tag = campo.get('tag')
        s = _subcampos(campo)
        if tag == '080':
            for bruto in s.get('a', []):
                m = re.match(r'\s*(\d[\d.\-+/:()=«»"\']*)', bruto or '')
                v = m.group(1).rstrip('.-+/:=') if m else ''
                if v and v not in datos['udc']:
                    datos['udc'].append(v)
        elif tag == '082':
            for bruto in s.get('a', []):
                n = normalizar_ddc(bruto)
                if n:
                    datos['ddc'].append(n)
            if s.get('2') and not datos['ddc_edicion']:
                datos['ddc_edicion'] = s['2'][0]
        elif tag == '245':
            partes = s.get('a', []) + s.get('b', [])
            datos['titulo'] = ' '.join(partes).rstrip(' /:;,.')
        elif tag == '100' and not datos['autor']:
            datos['autor'] = ' '.join(s.get('a', [])).rstrip(',. ')
        elif tag in ('264', '260'):
            if not datos['editorial'] and s.get('b'):
                datos['editorial'] = s['b'][0].rstrip(',:; ')
            if s.get('c'):
                datos['anio'] = _anio_de(s['c'][0]) or datos['anio']
    return datos


# ---------------------------- consultas SRU ----------------------------

def isbn_variantes(isbn):
    """Devuelve el ISBN ingresado más su equivalente en el otro formato (10 ⇄ 13).
    Muchos registros antiguos tienen cargado solo el ISBN-10: buscando ambos
    formatos a la vez se recuperan libros que con un solo formato no aparecen."""
    variantes = {isbn}
    try:
        if len(isbn) == 13 and isbn.startswith('978'):
            nucleo = isbn[3:12]                      # 9 dígitos centrales
            s = sum((10 - i) * int(d) for i, d in enumerate(nucleo))
            control = (11 - s % 11) % 11
            variantes.add(nucleo + ('X' if control == 10 else str(control)))
        elif len(isbn) == 10:
            nucleo = '978' + isbn[:9]
            s = sum((1 if i % 2 == 0 else 3) * int(d) for i, d in enumerate(nucleo))
            variantes.add(nucleo + str((10 - s % 10) % 10))
    except ValueError:
        pass
    return sorted(variantes)


def _cql(fuente, isbn, autor, titulo):
    partes = []
    idx = fuente['idx']
    if isbn:
        v = isbn_variantes(isbn)
        if len(v) > 1:
            partes.append('(' + ' or '.join('%s=%s' % (idx['isbn'], x) for x in v) + ')')
        else:
            partes.append('%s=%s' % (idx['isbn'], v[0]))
    # Folio (el catálogo de LC desde jul. 2025) no matchea dc.title/dc.author
    # con tildes: "túnel" da 0 resultados, "tunel" da los esperados. Las demás
    # fuentes (DNB, BNE, BIBNA, BNA) sí indexan con tilde y no se tocan.
    limpiar = _sin_tildes if fuente['clave'] == 'loc' else (lambda t: t)
    if autor:
        partes.append('%s="%s"' % (idx['autor'], limpiar(autor.replace('"', ''))))
    if titulo:
        partes.append('%s="%s"' % (idx['titulo'], limpiar(titulo.replace('"', ''))))
    return ' and '.join(partes)


def consultar_fuente(fuente, isbn, autor, titulo):
    """Consulta una fuente SRU. Nunca lanza excepción: informa su estado."""
    salida = {'fuente': fuente['nombre'], 'sigla': fuente['sigla'],
              'estado': 'ok', 'registros': 0, 'con_dewey': 0, 'con_cdu': 0, 'candidatos': [], 'candidatos_cdu': []}
    try:
        params = {'version': fuente.get('version', '1.1'), 'operation': 'searchRetrieve',
                  'query': _cql(fuente, isbn, autor, titulo),
                  'maximumRecords': '10', 'recordSchema': fuente['esquema']}
        r = requests.get(fuente['base'], params=params, headers=CABECERAS, timeout=TIEMPO_MAX)
        if r.status_code != 200:
            salida['estado'] = 'error'
            return salida
        registros = _registros_marc(r.text)
        salida['registros'] = len(registros)
        for reg in registros:
            d = parsear_bibliografico(reg)
            # En búsquedas por título, si hay autor conocido se exige coincidencia:
            # evita que obras ajenas con títulos parecidos (p. ej. «Las nuevas
            # venas abiertas…» de otro autor) se cuelen entre las ediciones.
            if not isbn and autor and not _coincide_autor(d['autor'], autor):
                continue
            base = {'ddc_edicion': d['ddc_edicion'], 'titulo': d['titulo'], 'autor': d['autor'],
                    'anio': d['anio'], 'editorial': d['editorial'], 'fuente': fuente['sigla']}
            for ddc in d['ddc']:
                salida['candidatos'].append(dict(base, ddc=ddc))
            for udc in d['udc']:
                salida['candidatos_cdu'].append(dict(base, ddc_edicion='', cdu=udc))
        salida['con_dewey'] = len({c['ddc'] for c in salida['candidatos']})
        salida['con_cdu'] = len({c['cdu'] for c in salida['candidatos_cdu']})
    except requests.Timeout:
        salida['estado'] = 'sin_respuesta'
    except Exception:
        salida['estado'] = 'error'
    return salida


# ---------------------------------------------------------------------------
# Fuentes Z39.50 (datos de conexión publicados por cada biblioteca)
# ---------------------------------------------------------------------------
FUENTES_Z = [
    {
        'clave': 'bibna', 'nombre': 'Biblioteca Nacional de Uruguay', 'sigla': 'BIBNA',
        'host': '164.73.2.157', 'puerto': 9992, 'base': 'BNU01',
        'usuario': 'Z39.50', 'clave_acceso': 'z39.bnu', 'activa': True,
    },
    {
        'clave': 'bna', 'nombre': 'Biblioteca Nacional Mariano Moreno (Argentina)', 'sigla': 'BNA',
        'host': '200.123.191.9', 'puerto': 9991, 'base': 'BNA01',
        'usuario': 'Z39.50', 'clave_acceso': 'Z39.50', 'activa': True,
    },
]

TIMEOUT_S = 30          # por fuente; Z39.50 sobre internet puede ser lento
MAX_REGISTROS = 5       # registros a pedir por búsqueda


# ---------------------------------------------------------------------------
# Cliente Z39.50 vía yaz-client
# ---------------------------------------------------------------------------
def _correr_yaz(fuente, consulta_rpn, archivo_marc, max_reg=MAX_REGISTROS):
    """Ejecuta yaz-client con un guion de comandos; los registros crudos
    quedan volcados en archivo_marc (formato ISO 2709)."""
    guion = (
        f"set_marcdump {archivo_marc}\n"
        f"auth {fuente['usuario']}/{fuente['clave_acceso']}\n"
        f"open tcp:{fuente['host']}:{fuente['puerto']}/{fuente['base']}\n"
        f"find {consulta_rpn}\n"
        f"show 1+{max_reg}\n"
        f"close\nquit\n"
    )
    return subprocess.run(
        ['yaz-client'], input=guion, capture_output=True,
        text=True, timeout=TIMEOUT_S,
    )


def _parsear_marc(archivo_marc):
    """Lee los registros ISO 2709 volcados por yaz-client y extrae los campos
    útiles: CDU (080), Dewey (082), título, autor, año, editorial."""
    registros = []
    if not os.path.exists(archivo_marc) or os.path.getsize(archivo_marc) == 0:
        return registros
    with open(archivo_marc, 'rb') as f:
        # to_unicode + force_utf8: ambas bibliotecas declaran UTF-8
        for reg in MARCReader(f, to_unicode=True, force_utf8=True, utf8_handling='replace'):
            if reg is None:
                continue
            d = {'ddc': [], 'udc': [], 'ddc_edicion': '',
                 'titulo': '', 'autor': '', 'anio': '', 'editorial': ''}
            for campo in reg.get_fields('080'):
                for v in campo.get_subfields('a'):
                    # Algunos registros traen la notación con texto pegado
                    # ("929Duhalde, Eduardo..."): se conserva solo la notación
                    # CDU válida del comienzo (dígitos y sus auxiliares).
                    m = re.match(r'\s*(\d[\d.\-+/:()=«»"\']*)', v or '')
                    v = m.group(1).rstrip('.-+/:=') if m else ''
                    if v and v not in d['udc']:
                        d['udc'].append(v)
            for campo in reg.get_fields('082'):
                for v in campo.get_subfields('a'):
                    v = (v or '').replace('/', '').strip()
                    v = v.split()[0] if v.split() else ''
                    v = v.rstrip('.')
                    if re.match(r'^\d{3}(\.\d+)?$', v) and v not in d['ddc']:
                        d['ddc'].append(v)
                if not d['ddc_edicion']:
                    ed = campo.get_subfields('2')
                    if ed:
                        d['ddc_edicion'] = (ed[0] or '').strip()
            t = reg.get_fields('245')
            if t:
                partes = t[0].get_subfields('a', 'b')
                d['titulo'] = ' '.join(p.strip(' /:;') for p in partes if p).strip()
            a = reg.get_fields('100') or reg.get_fields('700')
            if a:
                d['autor'] = (a[0].get_subfields('a') or [''])[0].strip(' ,')
            for tag in ('264', '260'):
                pub = reg.get_fields(tag)
                if pub:
                    ed = (pub[0].get_subfields('b') or [''])[0].strip(' ,:;')
                    an = (pub[0].get_subfields('c') or [''])[0]
                    d['editorial'] = d['editorial'] or ed
                    m = re.search(r'(1[89]\d\d|20\d\d)', an or '')
                    d['anio'] = d['anio'] or (m.group(1) if m else '')
                    break
            registros.append(d)
    return registros


def _sin_tildes(texto):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', texto or '')
                   if unicodedata.category(c) != 'Mn').lower()


def _coincide_autor(registro_autor, autor_buscado):
    """True si el apellido buscado aparece en el autor del registro
    (sin distinguir tildes ni mayúsculas). Con autor vacío no filtra."""
    if not autor_buscado:
        return True
    apellido = _sin_tildes(autor_buscado).replace(',', ' ').split()
    return bool(apellido) and apellido[0] in _sin_tildes(registro_autor)


def _limpiar_rpn(texto):
    """Quita comillas y caracteres que romperían la consulta RPN."""
    return re.sub(r'["\\]', ' ', texto or '').strip()


def obra_por_openlibrary(isbn):
    """Si ninguna biblioteca tiene el ISBN, se le pregunta a Open Library
    qué obra es (título y autor) para poder buscar otras ediciones."""
    try:
        claves = ','.join('ISBN:' + v for v in isbn_variantes(isbn))
        r = requests.get('https://openlibrary.org/api/books',
                         params={'bibkeys': claves, 'format': 'json', 'jscmd': 'data'},
                         timeout=10)
        for v in r.json().values():
            titulo = (v.get('title') or '').strip()
            autores = v.get('authors') or []
            nombre = (autores[0].get('name') if autores else '') or ''
            apellido = nombre.split()[-1] if nombre.split() else ''
            if titulo:
                return titulo, apellido
    except Exception:
        pass
    return '', ''


def consultar_fuente_z(fuente, isbn='', titulo='', autor='', via=''):
    salida = {'fuente': fuente['nombre'], 'sigla': fuente['sigla'],
              'estado': 'ok', 'registros': 0, 'hits': 0, 'con_dewey': 0, 'con_cdu': 0,
              'muestra': None, 'candidatos': [], 'candidatos_cdu': []}
    if not fuente.get('activa'):
        salida['estado'] = 'inactiva'
        return salida

    # Consultas RPN (atributos Bib-1): 7=ISBN, 4=título, 1003=autor
    consultas = []
    if isbn:
        consultas = [f'@attr 1=7 "{v}"' for v in isbn_variantes(isbn)]
    elif titulo and autor:
        consultas = [f'@and @attr 1=4 "{titulo}" @attr 1=1003 "{autor}"']
    elif titulo:
        consultas = [f'@attr 1=4 "{titulo}"']
    if not consultas:
        salida['estado'] = 'sin_consulta'
        return salida

    try:
        registros = []
        hits = 0
        for c in consultas:
            with tempfile.NamedTemporaryFile(suffix='.mrc', delete=False) as tmp:
                archivo = tmp.name
            try:
                proc = _correr_yaz(fuente, c, archivo, max_reg=(MAX_REGISTROS if isbn else 12))
                m = re.search(r'Number of hits:\s*(\d+)', proc.stdout or '')
                hits = max(hits, int(m.group(1)) if m else 0)
                registros = _parsear_marc(archivo)
            finally:
                try:
                    os.unlink(archivo)
                except OSError:
                    pass
            if registros:
                break  # con una variante del ISBN alcanzó
        # En búsquedas por título, si hay autor conocido se exige que coincida:
        # evita que un homónimo de otra persona se cuele entre las ediciones.
        if not isbn and autor:
            registros = [d for d in registros if _coincide_autor(d['autor'], autor)]
        salida['registros'] = len(registros)
        salida['hits'] = hits
        if registros:
            # muestra: metadatos del primer registro, aunque no traiga clasificación
            salida['muestra'] = {k: registros[0][k] for k in ('titulo', 'autor', 'anio', 'editorial')}
        for d in registros:
            base = {'ddc_edicion': d['ddc_edicion'], 'titulo': d['titulo'],
                    'autor': d['autor'], 'anio': d['anio'],
                    'editorial': d['editorial'], 'fuente': fuente['sigla']}
            if via:
                base['via'] = via
            for ddc in d['ddc']:
                salida['candidatos'].append(dict(base, ddc=ddc))
            for udc in d['udc']:
                salida['candidatos_cdu'].append(dict(base, ddc_edicion='', cdu=udc))
        salida['con_dewey'] = len({c['ddc'] for c in salida['candidatos']})
        salida['con_cdu'] = len({c['cdu'] for c in salida['candidatos_cdu']})
    except subprocess.TimeoutExpired:
        salida['estado'] = 'sin_respuesta'
    except Exception:
        salida['estado'] = 'error'
    return salida




def fusionar(resultados, lista='candidatos', clave='ddc'):
    """Agrupa los candidatos de todas las fuentes por número Dewey.
    El mismo número desde varias bibliotecas se muestra una vez, con todas sus fuentes."""
    grupos = {}
    for res in resultados:
        for c in res[lista]:
            g = grupos.setdefault(c[clave], {
                'via': c.get('via', ''),
                clave: c[clave], 'ddc_edicion': c['ddc_edicion'],
                'titulo': c['titulo'], 'autor': c['autor'],
                'anio': c['anio'], 'editorial': c['editorial'], 'fuentes': []})
            if not c.get('via'):
                g['via'] = ''
            if c['fuente'] not in g['fuentes']:
                g['fuentes'].append(c['fuente'])
            # completar huecos con datos de otra fuente
            for k in ('titulo', 'autor', 'anio', 'editorial', 'ddc_edicion'):
                if not g[k] and c[k]:
                    g[k] = c[k]
    # más fuentes coincidentes primero; a igualdad, número más específico primero
    return sorted(grupos.values(), key=lambda g: (g.get('via') == 'obra', -len(g['fuentes']), -len(g[clave])))


# ---------------------------- rutas ----------------------------

@app.route('/')
def portada():
    return send_file(os.path.join(os.path.dirname(__file__), 'index.html'))


@app.route('/api/ping')
def api_ping():
    """Despertador: la página cliente lo llama al cargar para que el servicio
    salga de la siesta del plan gratuito antes de la primera consulta real."""
    return jsonify({'ok': True})


@app.route('/api/fuentes')
def api_fuentes():
    return jsonify([{'nombre': f['nombre'], 'sigla': f['sigla'], 'activa': f['activa'],
                     'nota': f.get('nota', '')} for f in FUENTES])


@app.route('/api/dewey')
def api_dewey():
    isbn = re.sub(r'[-\s]', '', request.args.get('isbn', '').strip())
    autor = request.args.get('autor', '').strip()
    titulo = request.args.get('titulo', '').strip()
    if isbn and not re.match(r'^(\d{9}[\dXx]|\d{13})$', isbn):
        return jsonify({'error': 'El ISBN debe tener 10 o 13 dígitos.'}), 400
    if not (isbn or autor or titulo):
        return jsonify({'error': 'Ingresá un ISBN, o autor y título.'}), 400
    activas = [f for f in FUENTES if f['activa']] + \
              [f for f in FUENTES_Z if f.get('activa')]

    def consultar(f, isbn_c='', titulo_c='', autor_c='', via=''):
        """Puente único: elige el protocolo según la fuente y etiqueta la vía."""
        if 'host' in f:  # Z39.50
            return consultar_fuente_z(f, isbn_c, titulo_c, autor_c, via=via)
        r = consultar_fuente(f, isbn_c, autor_c, titulo_c)
        if via:
            for c in r['candidatos'] + r['candidatos_cdu']:
                c['via'] = via
        r.setdefault('hits', r.get('registros', 0))
        r.setdefault('muestra', None)
        return r

    with ThreadPoolExecutor(max_workers=len(activas)) as pool:
        resultados = list(pool.map(lambda f: consultar(f, isbn, titulo, autor), activas))

    # --- Cascada obra/edición: si una biblioteca quedó sin clasificación (no
    # halló la edición, o la halló vacía de 080/082), se identifica la obra y
    # se le vuelve a preguntar por cualquier edición. La clasificación es de
    # la obra, no de la edición. ---
    obra = None
    if isbn:
        pendientes_i = [i for i, r in enumerate(resultados)
                        if r['estado'] == 'ok'
                        and not r['candidatos'] and not r['candidatos_cdu']]
        if pendientes_i:
            t_obra, a_obra = titulo, autor
            if not t_obra:
                con_muestra = next((r for r in resultados if r.get('muestra')), None)
                if con_muestra:
                    t_obra = con_muestra['muestra']['titulo']
                    a_obra = a_obra or (con_muestra['muestra']['autor'].split(',')[0]
                                        if con_muestra['muestra']['autor'] else '')
            if not t_obra:
                # último recurso: identificar la obra por los propios candidatos ya hallados
                con_cand = next((r for r in resultados if r['candidatos'] or r['candidatos_cdu']), None)
                if con_cand:
                    c0 = (con_cand['candidatos'] + con_cand['candidatos_cdu'])[0]
                    t_obra = c0['titulo']
                    a_obra = a_obra or (c0['autor'].split(',')[0] if c0['autor'] else '')
            if not t_obra:
                t_obra, a_obra = obra_por_openlibrary(isbn)
            t_obra, a_obra = _limpiar_rpn(t_obra), _limpiar_rpn(a_obra)
            if t_obra:
                obra = {'titulo': t_obra.strip(' .'), 'autor': a_obra}
                pendientes = [activas[i] for i in pendientes_i]
                with ThreadPoolExecutor(max_workers=len(pendientes)) as pool:
                    por_obra = list(pool.map(
                        lambda f: consultar(f, '', t_obra, a_obra, via='obra'), pendientes))
                for i, res_obra in zip(pendientes_i, por_obra):
                    if res_obra['candidatos'] or res_obra['candidatos_cdu']:
                        r = resultados[i]
                        r['candidatos'] += res_obra['candidatos']
                        r['candidatos_cdu'] += res_obra['candidatos_cdu']
                        r['registros'] += res_obra['registros']
                        r['hits_obra'] = res_obra.get('hits', res_obra['registros'])
                        r['con_dewey'] = len({c['ddc'] for c in r['candidatos']})
                        r['con_cdu'] = len({c['cdu'] for c in r['candidatos_cdu']})

    return jsonify({'obra': obra,
                    'fuentes': [{k: r.get(k) for k in ('fuente', 'sigla', 'estado', 'registros',
                                                       'con_dewey', 'con_cdu')} |
                                {'hits_obra': r.get('hits_obra', 0)}
                                for r in resultados],
                    'candidatos': fusionar(resultados),
                    'candidatos_cdu': fusionar(resultados, 'candidatos_cdu', 'cdu')})


@app.route('/api/autoridad')
def api_autoridad():
    """Verifica un autor contra el archivo de autoridades de nombres (NAF) de la LoC.
    Recomendación de la LoC: apellido primero ("Rivas, Manuel")."""
    nombre = request.args.get('nombre', '').strip()
    if not nombre:
        return jsonify({'error': 'Ingresá un nombre.'}), 400
    try:
        params = {'version': '1.1', 'operation': 'searchRetrieve',
                  'query': 'bath.personalName="%s"' % nombre.replace('"', ''),
                  'maximumRecords': '8', 'recordSchema': 'marcxml'}
        r = requests.get(NAF_BASE, params=params, headers=CABECERAS, timeout=TIEMPO_MAX)
        if r.status_code != 200:
            return jsonify({'estado': 'error', 'formas': []})
        formas = []
        for reg in _registros_marc(r.text):
            forma, variantes = '', []
            for campo in reg:
                if _local(campo.tag) != 'datafield':
                    continue
                s = _subcampos(campo)
                encab = ' '.join(sum((s.get(c, []) for c in ('a', 'b', 'c', 'd')), [])).strip(' ,.')
                if campo.get('tag') == '100' and not forma:
                    forma = encab
                elif campo.get('tag') == '400' and encab and len(variantes) < 3:
                    variantes.append(encab)
            if forma and forma not in [f['forma'] for f in formas]:
                formas.append({'forma': forma, 'variantes': variantes})
        return jsonify({'estado': 'ok', 'formas': formas[:8]})
    except Exception:
        return jsonify({'estado': 'sin_respuesta', 'formas': []})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))