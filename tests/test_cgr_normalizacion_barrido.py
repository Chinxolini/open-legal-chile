"""Normalizacion de numeros de ley y barrido de paginas del conector CGR, sin red.

La API se reemplaza por un portal falso que sirve paginas armadas a mano; la cache TTL y
``_normalizar_hit`` corren de verdad. Las pruebas en vivo estan en test_cgr_regresion.py.
"""
import io
import json
import urllib.error

import pytest

import cgr_connector
from cgr_connector import CGRClient, _cita_alguna_ley, _numeros_de_ley, _variantes_consulta


def _hit(doc_id, fuentes="", texto="", materia=""):
    return {"_id": doc_id, "_source": {"doc_id": doc_id, "fuentes_legales": fuentes,
                                       "documento_completo": texto, "materia": materia,
                                       "fecha_documento": "2024-05-02T00:00:00"}}


def _cita(doc_id):
    """Un dictamen que menciona la Ley 21.180 en sus fuentes."""
    return _hit(doc_id, fuentes="ley 21180 art/1", texto="Aplica la Ley N° 21.180.")


class _PortalFalso:
    """Sirve {(consulta, pagina): [hits] | Exception} y registra cada llamada."""

    def __init__(self, paginas, totales=None, por_defecto=None, secuencias=None):
        self.paginas = paginas
        self.secuencias = {k: list(v) for k, v in (secuencias or {}).items()}  # fallas antes de responder
        self.totales = totales or {}
        self.por_defecto = por_defecto
        self.llamadas = []

    def __call__(self, req, timeout=30):
        cuerpo = json.loads(req.data.decode("utf-8"))
        clave = (cuerpo["search"], cuerpo["page"])
        self.llamadas.append(clave)
        if self.secuencias.get(clave):
            raise self.secuencias[clave].pop(0)
        pagina = self.paginas.get(clave)
        if pagina is None and self.por_defecto:
            pagina = self.por_defecto(*clave)
        if isinstance(pagina, Exception):
            raise pagina
        if isinstance(pagina, dict):  # respuesta cruda (p. ej. error de Elasticsearch)
            return io.BytesIO(json.dumps(pagina).encode("utf-8"))
        pagina = pagina or []
        total = self.totales.get(cuerpo["search"])
        if total is None:  # por defecto, coherente con las paginas armadas (o "muchos" si son infinitas)
            total = 10_000 if self.por_defecto else sum(
                len(v) for (q, _), v in self.paginas.items() if q == cuerpo["search"] and isinstance(v, list))
        crudo = {"hits": {"total": {"value": total}, "hits": pagina}}
        return io.BytesIO(json.dumps(crudo).encode("utf-8"))


@pytest.fixture
def pausas(monkeypatch):
    registro = []
    monkeypatch.setattr(cgr_connector.time, "sleep", registro.append)
    return registro


@pytest.fixture
def cliente(tmp_path, monkeypatch, pausas):
    def _armar(portal):
        monkeypatch.setattr(cgr_connector, "safe_urlopen", portal)
        return CGRClient(cache_dir=str(tmp_path))
    return _armar


def _ids(res):
    return [r["docId"] for r in res["resultados"]]


# ── 1. Normalizacion ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("consulta, esperadas", [
    ("21180", ["21180", "21.180"]),
    ("21.180", ["21.180", "21180"]),
    ("Ley 21.180", ["Ley 21.180", "Ley 21180"]),
    ("Ley N° 21.180", ["Ley N° 21.180", "Ley N° 21180"]),
    ("ley Nº21180", ["ley Nº21180", "ley Nº21.180"]),
    ("21.180 bis", ["21.180 bis", "21180 bis"]),
    ("transformación digital 21180", ["transformación digital 21180", "transformación digital 21.180"]),
    ("21180 y 19.880", ["21180 y 19.880", "21180 y 19880", "21.180 y 19.880"]),
    ("99999", ["99999", "99.999"]),
    ("10000", ["10000", "10.000"]),
    # 4 digitos: solo con marcador de norma
    ("2023", ["2023"]),
    ("dictámenes 2023", ["dictámenes 2023"]),
    ("ley 2023", ["ley 2023", "ley 2.023"]),
    ("Ley N° 3.500", ["Ley N° 3.500", "Ley N° 3500"]),
    ("DL 3500", ["DL 3500", "DL 3.500"]),
    ("D.F.L. 1.122", ["D.F.L. 1.122", "D.F.L. 1122"]),
    ("decreto ley 2.186", ["decreto ley 2.186", "decreto ley 2186"]),
    # no son numeros de ley
    ("100000", ["100000"]),
    ("100.000", ["100.000"]),
    ("1.021.180", ["1.021.180"]),
    ("021180", ["021180"]),
    ("E311060N23", ["E311060N23"]),
    ("21,5", ["21,5"]),
    ("21.18", ["21.18"]),
    ("D.F.L. 1", ["D.F.L. 1"]),
    ("probidad administrativa", ["probidad administrativa"]),
    ("", [""]),
])
def test_variantes_consulta(consulta, esperadas):
    assert _variantes_consulta(consulta) == esperadas


def test_numeros_de_ley_devuelve_digitos_sin_punto():
    assert [d for _, _, d in _numeros_de_ley("Ley N° 21.180 y ley 19880")] == ["21180", "19880"]
    assert _numeros_de_ley("dictamen E311060N23 de 2023") == []


@pytest.mark.parametrize("campos, numeros, esperado", [
    ({"fuentesLegales": "ley 21180 art/nov"}, ["21180"], True),
    ({"texto": "en virtud de la Ley N° 21.180, sobre transformación digital"}, ["21180"], True),
    ({"texto": "conforme a las leyes Nos 19.880, 19.799 y 21.180, por lo que"}, ["21180"], True),
    ({"texto": "Firma; y, 21.180, que, entre otros"}, ["21180"], True),
    # el correlativo del propio dictamen no es una mencion de la ley
    ({"docId": "021180N84", "numero": "021180", "texto": "concesion de radiodifusion"}, ["21180"], False),
    ({"texto": "se pagó la suma de $21.180 al proveedor"}, ["21180"], False),
    ({"texto": "según el dictamen N° 21.180, de 1990"}, ["21180"], False),
    ({"texto": "el oficio Nº 21180 de la subsecretaría"}, ["21180"], False),
    ({"texto": "la ley 121.180 no existe"}, ["21180"], False),
    ({"texto": "la ley 21.1805 no existe"}, ["21180"], False),
    ({"materia": ""}, ["21180"], False),
    # bajo 10.000 se exige el marcador
    ({"texto": "conforme al DL N° 3.500"}, ["3500"], True),
    ({"texto": "en el año 2023 se dictó"}, ["2023"], False),
    ({"texto": "la ley 2023 citada"}, ["2023"], True),
])
def test_cita_alguna_ley(campos, numeros, esperado):
    assert _cita_alguna_ley(campos, numeros) is esperado


# ── 2. Union y dedup ─────────────────────────────────────────────────────────────────────────

def test_union_por_doc_id_conserva_el_orden(cliente):
    portal = _PortalFalso({
        ("21.180", 0): [_cita("A"), _cita("B")],
        ("21180", 0): [_cita("B"), _cita("C"), _hit("021180N84", texto="radiodifusion"), _cita("A")],
    }, totales={"21.180": 2, "21180": 4})
    res = cliente(portal).search_jurisprudencia("21.180")
    assert _ids(res) == ["A", "B", "C"], "primero la consulta original, luego lo nuevo verificado"
    assert res["variantes"] == ["21.180", "21180"]
    assert res["totales_por_variante"] == {"21.180": 2, "21180": 4}
    assert res["total"] == 3, "con todo leido, el total es el de la union"
    assert portal.llamadas == [("21.180", 0), ("21180", 0)]


def test_lo_que_trae_la_consulta_original_no_se_filtra(cliente):
    """La consulta del usuario manda: el filtro solo aplica a lo que agrega una variante."""
    portal = _PortalFalso({
        ("21180", 0): [_hit("021180N84", texto="radiodifusion")],
        ("21.180", 0): [_cita("X")],
    })
    assert _ids(cliente(portal).search_jurisprudencia("21180")) == ["021180N84", "X"]


def test_sin_numero_y_sin_opciones_la_respuesta_es_la_de_siempre(cliente):
    portal = _PortalFalso({("probidad", 0): [_hit("P1")]}, totales={"probidad": 3080})
    res = cliente(portal).search_jurisprudencia("probidad")
    assert set(res) == {"query", "source", "total", "page", "resultados"}
    assert res["total"] == 3080 and _ids(res) == ["P1"]
    assert portal.llamadas == [("probidad", 0)]


def test_page_explicito_se_respeta(cliente):
    portal = _PortalFalso({("probidad", 3): [_hit("P61")]})
    res = cliente(portal).search_jurisprudencia("probidad", page=3)
    assert res["page"] == 3 and portal.llamadas == [("probidad", 3)]


def test_exact_y_normalizar_false_no_agregan_variantes(cliente):
    portal = _PortalFalso({("21.180", 0): [_cita("A")]})
    c = cliente(portal)
    c.search_jurisprudencia("21.180", exact=True)
    c.search_jurisprudencia("21.180", normalizar_numeros=False, use_cache=False)
    assert portal.llamadas == [("21.180", 0), ("21.180", 0)]


def test_get_dictamen_no_normaliza(cliente):
    """El identificador va tal cual: nunca se consulta una variante del numero."""
    portal = _PortalFalso({("E311060N23", 0): [_hit("E311060N23")]})
    assert cliente(portal).get_dictamen("E311060N23")["docId"] == "E311060N23"
    assert {q for q, _ in portal.llamadas} == {"E311060N23"}


def test_auditorias_e_instructivos_aceptan_las_opciones(cliente):
    portal = _PortalFalso({}, por_defecto=lambda q, p: [_cita(f"{q}-{p}")] if p == 0 else [],
                          totales={"probidad": 1, "ley 21.180": 1, "ley 21180": 1})
    c = cliente(portal)
    assert len(c.search_auditorias("ley 21.180", limit=1)["resultados"]) == 1
    assert c.search_instructivos("probidad", todas_las_paginas=True)["barrido_completo"] is True


# ── 3. Barrido de paginas ────────────────────────────────────────────────────────────────────

def _pagina(prefijo, n):
    return [_hit(f"{prefijo}{i}") for i in range(n)]


def test_barrido_termina_con_la_ultima_pagina_incompleta(cliente):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20), ("probidad", 1): _pagina("b", 20),
                           ("probidad", 2): _pagina("c", 5)})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    assert len(res["resultados"]) == 45 and res["total"] == 45
    assert res["barrido_completo"] is True and res["paginas_consultadas"] == 3
    assert "advertencias" not in res


def test_exactamente_20_con_total_20_no_pide_otra_pagina(cliente):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20)}, totales={"probidad": 20})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    assert portal.llamadas == [("probidad", 0)]
    assert res["barrido_completo"] is True and res["total"] == 20


def test_exactamente_20_y_luego_vacia(cliente):
    """Si el total informado no cuadra, la pagina vacia cierra el barrido y se avisa lo que falta."""
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20)}, totales={"probidad": 999})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    assert portal.llamadas == [("probidad", 0), ("probidad", 1)]
    assert res["barrido_completo"] is False and len(res["resultados"]) == 20
    assert "faltan 979 de 999" in res["advertencias"][0] and res["total"] == 999


def test_orden_inestable_se_detecta(cliente):
    """Medido en "probidad": entre paginas la API repite documentos y otros no aparecen nunca."""
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20),
                           ("probidad", 1): _pagina("a", 2) + _pagina("b", 18),
                           ("probidad", 2): _pagina("c", 5)}, totales={"probidad": 45})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    assert len(res["resultados"]) == 43
    assert portal.llamadas[3:] == [("probidad", 0), ("probidad", 1)], "se repite la frontera, sin cache"
    assert res["barrido_completo"] is False and res["total"] == 45
    assert "repitió 2" in res["advertencias"][0] and "faltan 2 de 45" in res["advertencias"][0]


def test_orden_inestable_se_recupera_repitiendo_la_frontera(cliente):
    vueltas = {"p1": 0}

    def portal_inestable(q, p):
        if p == 0:
            return _pagina("a", 20)
        if p == 1:
            vueltas["p1"] += 1  # la primera vez trae 2 repetidos de la pagina 0 en vez de b18 y b19
            return _pagina("a", 2) + _pagina("b", 18) if vueltas["p1"] == 1 else _pagina("b", 20)
        return _pagina("c", 5)

    portal = _PortalFalso({}, por_defecto=portal_inestable, totales={"probidad": 45})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    assert len(res["resultados"]) == 45 == len(set(_ids(res))) == res["total"]
    assert res["barrido_completo"] is True and "advertencias" not in res
    assert portal.llamadas == [("probidad", 0), ("probidad", 1), ("probidad", 2), ("probidad", 0), ("probidad", 1)]


def test_pagina_repetida_corta_el_barrido(cliente):
    repetida = _pagina("a", 20)
    portal = _PortalFalso({}, por_defecto=lambda q, p: repetida)
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True, max_paginas=50)
    assert len(portal.llamadas) == 2, "no puede seguir pidiendo paginas que se repiten"
    assert len(res["resultados"]) == 20
    assert res["barrido_completo"] is False
    assert "repitió" in res["advertencias"][0]


def test_tope_de_paginas_y_techo(cliente):
    portal = _PortalFalso({}, por_defecto=lambda q, p: _pagina(f"p{p}-", 20))
    c = cliente(portal)
    res = c.search_jurisprudencia("probidad", todas_las_paginas=True, max_paginas=3)
    assert len(portal.llamadas) == 3 and len(res["resultados"]) == 60
    assert res["barrido_completo"] is False and res["total"] == 10_000

    portal.llamadas.clear()
    c.search_jurisprudencia("otra", todas_las_paginas=True, max_paginas=10_000)
    assert len(portal.llamadas) == cgr_connector._TECHO_PAGINAS

    portal.llamadas.clear()
    c.search_jurisprudencia("una mas", todas_las_paginas=True, max_paginas=0)
    assert len(portal.llamadas) == 1


def test_tope_es_por_variante(cliente):
    portal = _PortalFalso({}, por_defecto=lambda q, p: [_cita(f"{q}-{p}-{i}") for i in range(20)])
    res = cliente(portal).search_jurisprudencia("21.180", todas_las_paginas=True, max_paginas=2)
    assert portal.llamadas == [("21.180", 0), ("21.180", 1), ("21180", 0), ("21180", 1)]
    assert len(res["resultados"]) == 80


def test_error_a_mitad_conserva_lo_reunido(cliente):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20),
                           ("probidad", 1): urllib.error.URLError("portal caido")}, totales={"probidad": 60})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    assert len(res["resultados"]) == 20
    assert res["barrido_completo"] is False and "portal caido" in res["advertencias"][0]


def test_error_json_de_la_api_no_se_lee_como_fin(cliente, tmp_path):
    """Elasticsearch responde 'Result window is too large' como JSON: no es un barrido completo."""
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20),
                           ("probidad", 1): {"error": {"type": "illegal_argument_exception"}, "status": 400}},
                          totales={"probidad": 60})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    assert res["barrido_completo"] is False and "error" in res["advertencias"][0]
    assert not (tmp_path / "dictamenes_probidad_p1.json").exists(), "el error no debe quedar en cache"


def test_error_en_la_primera_pagina_se_propaga(cliente):
    portal = _PortalFalso({("probidad", 0): urllib.error.URLError("portal caido")})
    with pytest.raises(urllib.error.URLError):
        cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)


def test_error_en_la_variante_conserva_la_original(cliente):
    portal = _PortalFalso({("21.180", 0): [_cita("A")], ("21180", 0): TimeoutError("lento")})
    res = cliente(portal).search_jurisprudencia("21.180")
    assert _ids(res) == ["A"] and "lento" in res["advertencias"][0]


def test_limit_detiene_el_barrido(cliente):
    portal = _PortalFalso({}, por_defecto=lambda q, p: [_cita(f"{q}-{p}-{i}") for i in range(20)])
    res = cliente(portal).search_jurisprudencia("21.180", todas_las_paginas=True, limit=25)
    assert len(res["resultados"]) == 25
    assert portal.llamadas == [("21.180", 0), ("21.180", 1)], "con el limite cubierto no se consulta la variante"
    assert res["barrido_completo"] is False


def test_limit_sin_barrido_recorta_una_pagina(cliente):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20)})
    res = cliente(portal).search_jurisprudencia("probidad", limit=5)
    assert _ids(res) == [f"a{i}" for i in range(5)] and portal.llamadas == [("probidad", 0)]


def test_limit_no_positivo_se_ignora(cliente):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20)})
    assert len(cliente(portal).search_jurisprudencia("probidad", limit=0)["resultados"]) == 20


def test_pausa_entre_llamadas_de_red_y_no_con_cache(cliente, pausas):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20), ("probidad", 1): _pagina("b", 20),
                           ("probidad", 2): _pagina("c", 3)})
    c = cliente(portal)
    c.search_jurisprudencia("probidad", todas_las_paginas=True)
    assert pausas == [cgr_connector._PAUSA_ENTRE_PAGINAS] * 2
    pausas.clear()
    res = c.search_jurisprudencia("probidad", todas_las_paginas=True)
    assert pausas == [] and len(portal.llamadas) == 3, "la segunda vuelta sale entera de la cache"
    assert len(res["resultados"]) == 43


# ── 4. Servidor MCP ──────────────────────────────────────────────────────────────────────────

class _CGRRegistrador:
    def __init__(self):
        self.kwargs = []

    def search_jurisprudencia(self, query, **k):
        self.kwargs.append(k)
        return {"query": query, "total": 0, "resultados": []}

    def search_auditorias(self, query, **k):
        self.kwargs.append(k)
        return {"query": query, "total": 0, "resultados": []}


def test_mcp_pasa_las_opciones_solo_si_vienen(monkeypatch):
    import mcp_server
    falso = _CGRRegistrador()
    monkeypatch.setattr(mcp_server, "cgr", falso)
    mcp_server.handle_tool_call("cgr_search_jurisprudencia", {"query": "ley 21.180"})
    mcp_server.handle_tool_call("cgr_search_jurisprudencia",
                                {"query": "ley 21.180", "todas_las_paginas": True, "limit": "30"})
    mcp_server.handle_tool_call("cgr_search_auditorias", {"query": "hospital", "limit": 5})
    assert falso.kwargs == [{}, {"todas_las_paginas": True, "limit": 30}, {"limit": 5}]


def test_mcp_esquema_expone_las_opciones():
    from mcp_server import TOOLS
    for nombre in ("cgr_search_jurisprudencia", "cgr_search_auditorias"):
        esquema = next(t for t in TOOLS if t["name"] == nombre)["inputSchema"]
        assert esquema["properties"]["todas_las_paginas"]["type"] == "boolean"
        assert esquema["properties"]["limit"]["type"] == "integer"
        assert esquema["required"] == ["query"]


# ── 5. Barrido automatico acotado ────────────────────────────────────────────────────────────

def test_por_defecto_trae_el_universo_chico_entero(cliente):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20), ("probidad", 1): _pagina("b", 20),
                           ("probidad", 2): _pagina("c", 5)}, totales={"probidad": 45})
    res = cliente(portal).search_jurisprudencia("probidad", use_cache=False)
    assert len(res["resultados"]) == 45 and res["total"] == 45 and res["barrido_completo"] is True
    assert portal.llamadas == [("probidad", 0), ("probidad", 1), ("probidad", 2)], "la pagina 0 no se repite"


def test_por_defecto_universo_grande_es_una_pagina_como_antes(cliente):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20)}, totales={"probidad": 101})
    res = cliente(portal).search_jurisprudencia("probidad")
    assert set(res) == {"query", "source", "total", "page", "resultados"} and res["total"] == 101
    assert portal.llamadas == [("probidad", 0)]


def test_por_defecto_variante_grande_no_se_barre(cliente):
    portal = _PortalFalso({}, por_defecto=lambda q, p: [_cita(f"{q}-{p}-{i}") for i in range(20)],
                          totales={"21.180": 40, "21180": 500})
    res = cliente(portal).search_jurisprudencia("21.180")
    assert portal.llamadas == [("21.180", 0), ("21.180", 1), ("21180", 0)]
    assert res["barrido_completo"] is False and res["total"] == 500


def test_false_y_page_explicito_desactivan_el_automatico(cliente):
    portal = _PortalFalso({}, por_defecto=lambda q, p: _pagina(f"p{p}-", 20), totales={"probidad": 45})
    c = cliente(portal)
    c.search_jurisprudencia("probidad", todas_las_paginas=False)
    c.search_jurisprudencia("probidad", page=1)
    assert portal.llamadas == [("probidad", 0), ("probidad", 1)]


# ── 6. Backoff y registro de bloqueos ────────────────────────────────────────────────────────

def _http(code, retry_after=None):
    return urllib.error.HTTPError(cgr_connector.BASE_URL, code, "x",
                                  {"Retry-After": retry_after} if retry_after else {}, None)


def test_espera_backoff():
    espera = cgr_connector._espera_backoff
    assert [espera(i) for i in (1, 2, 3)] == [2.0, 4.0, 8.0]
    assert espera(10) == cgr_connector._BACKOFF_TECHO
    assert espera(1, "7") == 7.0 and espera(1, "999") == cgr_connector._BACKOFF_TECHO
    assert espera(2, "Wed, 21 Oct 2026 07:28:00 GMT") == 4.0, "fecha HTTP: se ignora y se usa el exponencial"


def test_429_se_reintenta_y_queda_registrado(cliente, pausas, caplog):
    portal = _PortalFalso({("probidad", 0): [_hit("P1")]}, secuencias={("probidad", 0): [_http(429, "3")]})
    c = cliente(portal)
    with caplog.at_level("WARNING", logger="openlegal.cgr"):
        res = c.search_jurisprudencia("probidad")
    assert _ids(res) == ["P1"] and pausas == [3.0]
    assert c.bloqueos == [{"ts": c.bloqueos[0]["ts"], "consulta": "probidad", "pagina": 0,
                           "motivo": "HTTP 429", "intento": 1, "espera_s": 3.0}]
    assert "HTTP 429" in caplog.text


def test_5xx_agota_los_reintentos_y_propaga(cliente, pausas):
    portal = _PortalFalso({}, secuencias={("probidad", 0): [_http(503)] * 4})
    c = cliente(portal)
    with pytest.raises(urllib.error.HTTPError):
        c.search_jurisprudencia("probidad")
    assert pausas == [2.0, 4.0, 8.0] and len(portal.llamadas) == 4
    assert [b["intento"] for b in c.bloqueos] == [1, 2, 3]


def test_5xx_agotado_con_copia_vencida(cliente, pausas, tmp_path):
    portal = _PortalFalso({("probidad", 0): [_hit("P1")]})
    c = cliente(portal)
    c.search_jurisprudencia("probidad")
    archivo = tmp_path / "dictamenes_probidad_p0.json"
    import os
    os.utime(archivo, (0, 0))  # copia vencida
    portal.secuencias[("probidad", 0)] = [_http(502)] * 4
    res = c.search_jurisprudencia("probidad")
    assert res.get("copia_local_vencida") is True and _ids(res) == ["P1"]


def test_4xx_no_se_reintenta(cliente, pausas):
    portal = _PortalFalso({}, secuencias={("probidad", 0): [_http(404)]})
    c = cliente(portal)
    with pytest.raises(urllib.error.HTTPError):
        c.search_jurisprudencia("probidad")
    assert pausas == [] and c.bloqueos == []


def test_timeout_se_reintenta_una_vez(cliente, pausas):
    portal = _PortalFalso({}, secuencias={("probidad", 0): [TimeoutError("lento"), TimeoutError("lento")]})
    c = cliente(portal)
    with pytest.raises(TimeoutError):
        c.search_jurisprudencia("probidad")
    assert len(portal.llamadas) == 2 and pausas == [2.0]
    assert c.bloqueos[0]["motivo"] == "TimeoutError"


def test_bloqueo_a_mitad_baja_el_ritmo(cliente, pausas):
    portal = _PortalFalso({("probidad", 0): _pagina("a", 20), ("probidad", 1): _pagina("b", 20),
                           ("probidad", 2): _pagina("c", 3)},
                          secuencias={("probidad", 1): [_http(429)]})
    res = cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True)
    pausa = cgr_connector._PAUSA_ENTRE_PAGINAS
    assert pausas == [pausa, 2.0, pausa * 2], "pausa, backoff y luego la pausa duplicada"
    assert len(res["resultados"]) == 43 and res["barrido_completo"] is True
    assert res["reintentos"][0]["motivo"] == "HTTP 429" and res["reintentos"][0]["pagina"] == 1


def test_pausa_adaptativa_tiene_techo(cliente, pausas):
    fallas = {("probidad", p): [_http(429)] for p in range(1, 8)}
    portal = _PortalFalso({}, por_defecto=lambda q, p: _pagina(f"p{p}-", 20), secuencias=fallas)
    cliente(portal).search_jurisprudencia("probidad", todas_las_paginas=True, max_paginas=8)
    entre_paginas = [x for x in pausas if x != 2.0]
    assert max(entre_paginas) == cgr_connector._PAUSA_MAXIMA


def test_registro_de_bloqueos_acotado(cliente, pausas):
    c = cliente(_PortalFalso({}))
    for i in range(cgr_connector._MAX_REGISTRO_BLOQUEOS + 30):
        c._registrar_bloqueo("q", i, "HTTP 429", 1, 2.0)
    assert len(c.bloqueos) == cgr_connector._MAX_REGISTRO_BLOQUEOS
    assert c.bloqueos[-1]["pagina"] == cgr_connector._MAX_REGISTRO_BLOQUEOS + 29


def test_mcp_pasa_false_explicito(monkeypatch):
    import mcp_server
    falso = _CGRRegistrador()
    monkeypatch.setattr(mcp_server, "cgr", falso)
    mcp_server.handle_tool_call("cgr_search_jurisprudencia", {"query": "probidad", "todas_las_paginas": False})
    assert falso.kwargs == [{"todas_las_paginas": False}]
