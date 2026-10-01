"""
Open Legal Chile — Conector Oficial Contraloría General de la República (CGR)
Módulo para consultar, indexar y buscar Jurisprudencia Administrativa, Dictámenes,
Instructivos y Auditorías vinculantes de la Contraloría General de la República de Chile.
"""

import os
import sys
import json
import re
import time
import logging
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from config import cache_fresco, leer_json_si_se_puede, safe_urlopen

BASE_URL = "https://www.contraloria.cl/apibusca"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cgr_cache")
_TTL_CACHE_SEGUNDOS = 30 * 24 * 60 * 60  # la jurisprudencia de la CGR cambia lento: un mes
_TAMANO_PAGINA = 20          # la API entrega 20 documentos por pagina y no deja cambiarlo
_MAX_PAGINAS_DEFECTO = 10    # barrido pedido: hasta 200 documentos por variante de la consulta
_TECHO_PAGINAS = 50          # tope duro aunque se pida mas (la API corta en from+size = 10.000)
_UMBRAL_BARRIDO_AUTOMATICO = 100  # por defecto se trae el universo entero si no pasa de 5 paginas
_PAGINAS_RECUPERACION = 6    # paginas que se repiten si el orden inestable dejo documentos fuera

# Ritmo y reintentos, medidos contra la API el 01-10-2026 (mediciones/ fuera del repo): ver PR.
_PAUSA_ENTRE_PAGINAS = 1.0   # segundos entre llamadas de red consecutivas de un barrido
_PAUSA_MAXIMA = 8.0          # tras cada bloqueo la pausa se duplica hasta este techo
_STATUS_REINTENTABLES = frozenset({429, 500, 502, 503, 504})
_REINTENTOS_HTTP = 3         # 429/5xx: esperas de 2, 4 y 8 s (o el Retry-After, hasta 60 s)
_REINTENTOS_RED = 1          # timeout o caida de red: un reintento (cada intento puede tardar 30 s)
_BACKOFF_BASE = 2.0
_BACKOFF_TECHO = 60.0
_MAX_REGISTRO_BLOQUEOS = 100

_log = logging.getLogger("openlegal.cgr")


def _espera_backoff(intento: int, retry_after: Optional[str] = None) -> float:
    """Espera antes del reintento ``intento`` (1, 2, 3...): exponencial, o el Retry-After del portal."""
    if retry_after and retry_after.strip().isdigit():
        return min(float(retry_after.strip()), _BACKOFF_TECHO)
    return min(_BACKOFF_BASE ** intento, _BACKOFF_TECHO)

# --- Normalizacion de numeros de ley ------------------------------------------------------------
# El indice de la CGR trata "21180" y "21.180" como terminos distintos: la misma consulta trae
# universos distintos segun como se escriba el numero (medido el 30-09-2026: 17 y 24 dictamenes,
# 16 en comun). Un numero de la consulta se trata como numero de ley si:
#   * esta aislado: no pegado a letras, digitos, puntos ni comas ("E311060N23", "1.021.180" y
#     "21,5" no califican) y no empieza con cero ("021180" es un correlativo de dictamen);
#   * vale entre 10.000 y 99.999, con o sin punto de miles ("21180", "21.180"); o
#   * vale entre 1.000 y 9.999 y lo precede un marcador de norma ("ley", "Ley N°", "DL",
#     "D.F.L.", "decreto ley"): "2023" a secas es mas probablemente un anio que una ley.
# Los sufijos se conservan: "21.180 bis" genera "21180 bis".
_MARCADOR_NORMA = (r"(?:\bley|\bd\.?\s?f?\.?\s?l\.?|\bdecreto\s+ley)\s*"
                   r"(?:(?:n|nro|num|número|numero)\s*[°º.]?\s*)?")
# Alfanumerico "de verdad": \w de Python incluye "º" (Nº21180) y lo pegaria al numero.
_ALNUM = "0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ_"
_RE_NUMERO = re.compile(rf"(?<![{_ALNUM}.,])([1-9]\d?\.\d{{3}}|[1-9]\d{{3,4}})(?![{_ALNUM}]|[.,]\d)")
_RE_MARCADOR_AL_FINAL = re.compile(_MARCADOR_NORMA + r"$", re.IGNORECASE)


def _numeros_de_ley(query: str) -> List[Tuple[int, int, str]]:
    """(inicio, fin, digitos sin punto) de cada numero de la consulta que califica como ley."""
    hallados = []
    for m in _RE_NUMERO.finditer(query):
        digitos = m.group(1).replace(".", "")
        if int(digitos) >= 10000 or _RE_MARCADOR_AL_FINAL.search(query[:m.start()]):
            hallados.append((m.start(), m.end(), digitos))
    return hallados


def _variantes_consulta(query: str) -> List[str]:
    """La consulta original primero; si trae numeros de ley, tambien la misma consulta con todos
    los numeros sin punto y con todos con punto de miles (a lo mas tres consultas)."""
    numeros = _numeros_de_ley(query)
    variantes = [query]
    for con_punto in (False, True):
        partes, cursor = [], 0
        for inicio, fin, digitos in numeros:
            partes.append(query[cursor:inicio])
            partes.append(f"{digitos[:-3]}.{digitos[-3:]}" if con_punto else digitos)
            cursor = fin
        partes.append(query[cursor:])
        variante = "".join(partes)
        if variante not in variantes:
            variantes.append(variante)
    return variantes


# Lo que precede a un numero que NO es una ley: un monto o el numero de otro acto.
_RE_OTRO_ACTO_AL_FINAL = re.compile(
    r"(?:\$|\b(?:dictamen(?:es)?|oficios?|ord\.?|resoluci[oó]n(?:es)?|decretos?|circular(?:es)?|rol)"
    r"\s*(?:exent[oa]s?\s*)?(?:(?:n|nro|num|número|numero)s?\s*[°º.]?\s*)?)\s*$",
    re.IGNORECASE,
)


def _cita_alguna_ley(item: Dict[str, Any], numeros: List[str]) -> bool:
    """¿El documento menciona alguna de las leyes de la consulta? Filtro para lo que solo trae una
    variante: el indice tambien calza el numero contra identificadores (consultar "21180" trae el
    dictamen 021180N84, que no menciona la Ley 21.180 en ninguna parte).

    Se busca en el contenido (fuentes legales, texto, materia, descriptores), nunca en ``docId`` ni
    ``numero``. Desde 10.000 basta el numero aislado, salvo que sea un monto ("$21.180") o el numero
    de otro acto ("dictamen N° 21.180"): asi entran las enumeraciones ("leyes Nos 19.880, 19.799 y
    21.180"). Bajo 10.000 se exige el marcador de norma ("ley 3.500"), porque "2023" suele ser un anio.
    """
    campos = " ".join(str(item.get(c) or "") for c in ("fuentesLegales", "texto", "materia", "descriptores"))
    for digitos in numeros:
        numero = re.escape(digitos[:-3]) + r"\.?" + re.escape(digitos[-3:])
        if int(digitos) < 10000:
            if re.search(_MARCADOR_NORMA + "0*" + numero + r"(?!\d|\.\d)", campos, re.IGNORECASE):
                return True
            continue
        for m in re.finditer(rf"(?<![{_ALNUM}.,])0*" + numero + rf"(?![{_ALNUM}]|[.,]\d)", campos):
            if not _RE_OTRO_ACTO_AL_FINAL.search(campos[max(0, m.start() - 40):m.start()]):
                return True
    return False


def _normalizar_hit(item: Dict[str, Any]) -> Dict[str, Any]:
    """Traduce un registro crudo del indice de la CGR al formato citable de la suite."""
    src = item.get("_source", {})
    # doc_id ("E311060N23") es el identificador citable: incluye el anio.
    # numeric_doc_id ("E311060") lo omite y no sirve para citar.
    # En auditorias el identificador citable es "número" ("460/2026"); numeric_doc_id
    # trae solo el correlativo ("460") y pierde el anio.
    doc_id = (src.get("doc_id") or src.get("número") or src.get("numero")
              or src.get("numeric_doc_id") or item.get("_id"))
    fecha = src.get("fecha_documento") or src.get("fecha_promulgación") or src.get("fecha") or ""
    fecha = fecha[:10] if len(fecha) >= 10 else fecha
    nombre = src.get("nombre") or src.get("title") or src.get("titulo") or ""
    conclusiones = src.get("conclusiones") or ""
    objetivo = src.get("objetivo") or ""
    # El texto integro viaja en documento_completo dentro de la propia respuesta
    # de busqueda: no hace falta un segundo viaje al lector HTML del portal.
    texto = (src.get("documento_completo") or src.get("texto_completo") or src.get("texto")
             or src.get("resumen") or conclusiones or objetivo or "")
    organismo = (src.get("destinatarios") or src.get("organismo")
                 or src.get("organismos_destinatarios") or src.get("servicio_") or "")
    tipo = src.get("_tipo") or "dictamenes"
    # Procedencia citable. Ademas, los dictamenes anteriores a 2000 no traen
    # documento_completo en el indice pero si se leen en esta URL.
    url_html = f"https://www.contraloria.cl/pdfbuscador/{tipo}/{doc_id}/html"
    pdf_url = src.get("pdf") or ""
    if pdf_url and not pdf_url.startswith("http"):
        pdf_url = f"https://www.contraloria.cl{pdf_url}"

    def _txt(valor: Any) -> str:
        return valor.strip() if isinstance(valor, str) else ""

    return {
        "docId": str(doc_id),
        "numero": _txt(src.get("n_dictamen") or src.get("número") or src.get("numeric_doc_id") or ""),
        "anio": str(src.get("year_doc_id") or fecha[:4]),
        "nombre": _txt(nombre),
        "fecha": fecha,
        "materia": _txt(src.get("materia") or src.get("resena") or src.get("descriptores") or nombre),
        "descriptores": _txt(src.get("descriptores")),
        "fuentesLegales": _txt(src.get("fuentes_legales")),
        "objetivo": _txt(objetivo),
        "conclusiones": _txt(conclusiones),
        "organismo": _txt(organismo),
        "texto": _txt(texto),
        "pdfUrl": pdf_url,
        "urlHtml": url_html,
    }


class _Barrido:
    """Estado de una busqueda de varias paginas y variantes: union por ``docId``, totales,
    advertencias y la pausa entre llamadas de red."""

    def __init__(self, cliente: "CGRClient", source: str, exact: bool, use_cache: bool,
                 tope: int, limite: Optional[int], umbral_automatico: Optional[int] = None):
        self.cliente, self.source, self.exact, self.use_cache = cliente, source, exact, use_cache
        self.tope, self.limite, self.umbral_automatico = tope, limite, umbral_automatico
        self.pausa = _PAUSA_ENTRE_PAGINAS
        self.semilla: Optional[Dict[str, Any]] = None  # pagina 0 ya obtenida de la primera variante
        self.reintentos: List[Dict[str, Any]] = []
        self.unidos: List[Dict[str, Any]] = []
        self.vistos: Set[str] = set()
        self.totales: Dict[str, int] = {}
        self.advertencias: List[str] = []
        self.completo = True
        self.paginas = 0
        self.hubo_red = False

    def lleno(self) -> bool:
        return bool(self.limite) and len(self.unidos) >= (self.limite or 0)

    def _pedir(self, variante: str, pagina: int, forzar_red: bool = False) -> Optional[Dict[str, Any]]:
        if self.semilla is not None:
            datos, self.semilla, self.hubo_red = self.semilla, None, True
            self.paginas += 1
            self.totales.setdefault(variante, int(datos.get("total") or 0))
            return datos
        if self.hubo_red:
            time.sleep(self.pausa)
        antes = len(self.cliente.bloqueos)
        try:
            datos, self.hubo_red = self.cliente._buscar_pagina(variante, self.source, pagina, self.exact,
                                                               self.use_cache and not forzar_red)
        except Exception as e:  # noqa: BLE001 — se informa y se conserva lo reunido
            if self.paginas == 0:
                raise  # sin nada reunido, el error es la respuesta (como antes)
            self.advertencias.append(f"'{variante}' página {pagina}: {e}")
            self.completo, self.hubo_red = False, True
            return None
        finally:
            nuevos = self.cliente.bloqueos[antes:]
            if nuevos:
                # El portal pidio aire: se baja el ritmo para el resto del barrido.
                self.reintentos.extend(nuevos)
                self.pausa = min(self.pausa * 2, _PAUSA_MAXIMA)
        self.paginas += 1
        if datos.get("copia_local_vencida"):
            self.advertencias.append(f"'{variante}' página {pagina}: copia local vencida, portal no disponible")
        self.totales.setdefault(variante, int(datos.get("total") or 0))
        return datos

    def ejecutar(self, query: str, variantes: List[str], page: int, forzar_detalle: bool) -> Dict[str, Any]:
        """Barre cada variante (la primera sin filtro, las demas solo si mencionan la ley) y arma la salida."""
        numeros = [d for _, _, d in _numeros_de_ley(query)]
        for indice, variante in enumerate(variantes):
            if self.lleno():
                self.completo = False
                break
            self.barrer(variante, page, (lambda it: True) if indice == 0 else
                        (lambda it: _cita_alguna_ley(it, numeros)))
        salida: Dict[str, Any] = {
            "query": query,
            "source": self.source,
            # Exacto si se leyo todo; si no, el mayor total informado (cota inferior de la union).
            "total": len(self.unidos) if self.completo else max(self.totales.values(), default=0),
            "page": page,
            "resultados": self.unidos[:self.limite] if self.limite else self.unidos,
        }
        if len(variantes) > 1:
            salida["variantes"] = variantes
            salida["totales_por_variante"] = self.totales
        if forzar_detalle or self.paginas > len(variantes):
            salida["paginas_consultadas"] = self.paginas
            salida["barrido_completo"] = self.completo
        for clave, valor in (("advertencias", self.advertencias), ("reintentos", self.reintentos)):
            if valor:
                salida[clave] = valor
        return salida

    def _incorporar(self, items: List[Dict[str, Any]], vistos_variante: Set[str],
                    admitir: Callable[[Dict[str, Any]], bool]) -> int:
        """Agrega lo nuevo de una pagina; devuelve cuantos documentos ya se habian visto."""
        repetidos = 0
        for it in items:
            if it["docId"] in vistos_variante:
                repetidos += 1
                continue
            vistos_variante.add(it["docId"])
            if it["docId"] not in self.vistos and admitir(it):
                self.vistos.add(it["docId"])
                self.unidos.append(it)
        return repetidos

    def _recuperar(self, variante: str, con_repetidos: List[int], vistos_variante: Set[str],
                   admitir: Callable[[Dict[str, Any]], bool]) -> None:
        """Vuelve a pedir, sin cache, las paginas donde hubo repetidos y la anterior a cada una.

        Medido el 01-10-2026: el indice ordena por (fecha, puntaje) y el puntaje varia entre
        peticiones, asi que documentos de la misma fecha cambian de pagina entre una llamada y otra:
        uno aparece dos veces y otro ninguna ("probidad": 2 de 3.080; "sumario administrativo":
        4 de 8.051). Repetir la frontera suele traer al que falto."""
        paginas = sorted({q for p in con_repetidos for q in (p - 1, p) if q >= 0})[:_PAGINAS_RECUPERACION]
        for pagina in paginas:
            if len(vistos_variante) >= self.totales[variante]:
                return
            datos = self._pedir(variante, pagina, forzar_red=True)
            if datos is None:
                return
            self._incorporar(datos.get("resultados", []), vistos_variante, admitir)

    def barrer(self, variante: str, desde: int, admitir: Callable[[Dict[str, Any]], bool]) -> None:
        """Recorre las paginas de una variante y agrega a la union lo nuevo que ``admitir`` acepte."""
        vistos_variante: Set[str] = set()
        repetidos, con_repetidos = 0, []
        for pagina in range(desde, desde + self.tope):
            datos = self._pedir(variante, pagina)
            if datos is None:
                return
            items = datos.get("resultados", [])
            if items and all(it["docId"] in vistos_variante for it in items):
                self.advertencias.append(
                    f"'{variante}' página {pagina}: la API repitió documentos ya vistos; barrido detenido")
                self.completo = False
                return
            ya_vistos = self._incorporar(items, vistos_variante, admitir)
            if ya_vistos:
                repetidos += ya_vistos
                con_repetidos.append(pagina)
            if len(items) < _TAMANO_PAGINA or len(vistos_variante) >= self.totales[variante]:
                if desde == 0 and con_repetidos and len(vistos_variante) < self.totales[variante]:
                    self._recuperar(variante, con_repetidos, vistos_variante, admitir)
                faltan = self.totales[variante] - len(vistos_variante) if desde == 0 else 0
                if faltan > 0:
                    self.advertencias.append(
                        f"'{variante}': la API repitió {repetidos} documento(s) entre páginas y faltan "
                        f"{faltan} de {self.totales[variante]} (orden inestable entre documentos de la misma fecha)")
                    self.completo = False
                return
            if self.umbral_automatico is not None and self.totales[variante] > self.umbral_automatico:
                self.completo = False  # universo grande: solo la primera pagina, salvo que se pida
                return
            if self.lleno():
                self.completo = False
                return
        self.completo = False  # se agoto el tope de paginas con documentos pendientes


class CGRClient:
    def __init__(self, cache_dir: str = CACHE_DIR):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        # Registro de reintentos (429, 5xx, timeouts) del cliente, el mas reciente al final.
        self.bloqueos: List[Dict[str, Any]] = []

    def _registrar_bloqueo(self, query: str, page: int, motivo: str, intento: int, espera: float) -> None:
        evento = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "consulta": query, "pagina": page,
                  "motivo": motivo, "intento": intento, "espera_s": espera}
        self.bloqueos.append(evento)
        del self.bloqueos[:-_MAX_REGISTRO_BLOQUEOS]
        _log.warning("CGR: %s en '%s' pagina %s; reintento %s en %.0f s", motivo, query, page, intento, espera)

    def _pedir_con_backoff(self, req: urllib.request.Request, query: str, page: int) -> str:
        """Hace la peticion con backoff exponencial ante 429, 5xx y fallas de red.

        Agotados los reintentos se propaga el ultimo error, como antes (y ``_buscar_pagina``
        recurre a la copia local vencida si la hay)."""
        intento = 0
        while True:
            try:
                with safe_urlopen(req, timeout=30) as resp:
                    return resp.read().decode("utf-8", errors="ignore")
            except urllib.error.HTTPError as e:
                if e.code not in _STATUS_REINTENTABLES or intento >= _REINTENTOS_HTTP:
                    raise
                motivo, retry_after = f"HTTP {e.code}", (e.headers.get("Retry-After") if e.headers else None)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if intento >= _REINTENTOS_RED:
                    raise
                motivo, retry_after = type(e).__name__, None
            intento += 1
            espera = _espera_backoff(intento, retry_after)
            self._registrar_bloqueo(query, page, motivo, intento, espera)
            time.sleep(espera)

    def _get_cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.json")

    def _buscar_pagina(self, query: str, source: str, page: int, exact: bool,
                       use_cache: bool) -> Tuple[Dict[str, Any], bool]:
        """Una pagina de la API con la cache TTL. Devuelve (datos, si hubo llamada de red).

        La API de la CGR es 0-indexada y entrega 20 documentos por página:
        ``page=1`` devuelve la SEGUNDA página, y por eso una consulta acotada
        respondía ``total`` mayor que cero con ``resultados`` vacío.
        """
        clean_q = query.strip().replace(" ", "_").lower()
        cache_key = f"{source}_{clean_q}_p{page}"
        cache_file = self._get_cache_path(cache_key)

        data = leer_json_si_se_puede(cache_file) if use_cache else None
        if data is not None and cache_fresco(cache_file, _TTL_CACHE_SEGUNDOS):
            return data, False

        url = f"{BASE_URL}/search/{source}"
        date_name = "fecha_promulgación" if source == "legislacion" else "fecha_documento"

        payload = {
            "search": query,
            "exact_search": exact,
            "options": {},
            "order": "desc",
            "date_name": date_name,
            "source": source,
            "page": page
        }

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "OpenLegalChile/1.0 (Derecho Administrativo Chile)"
            }
        )

        try:
            raw = self._pedir_con_backoff(req, query, page)
        except Exception:
            if data is not None:
                return {**data, "copia_local_vencida": True}, True
            raise
        res_json = json.loads(raw)
        if "hits" not in res_json and "error" in res_json:
            # Elasticsearch responde sus errores como JSON (p. ej. una pagina mas alla de la ventana
            # de 10.000): leerlo como lista vacia lo dejaba en cache y simulaba un barrido completo.
            raise RuntimeError(f"La API de la CGR respondio con error: {str(res_json['error'])[:200]}")

        hits = res_json.get("hits", {})
        total_val = hits.get("total", {})
        total_count = total_val.get("value", 0) if isinstance(total_val, dict) else total_val
        raw_items = hits.get("hits", [])

        clean_results = [_normalizar_hit(item) for item in raw_items]

        output_data = {
            "query": query,
            "source": source,
            "total": total_count,
            "page": page,
            "resultados": clean_results
        }

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)

        return output_data, True

    def search_jurisprudencia(self, query: str, source: str = "dictamenes", page: int = 0, exact: bool = False,
                              use_cache: bool = True, normalizar_numeros: bool = True,
                              todas_las_paginas: Optional[bool] = None, max_paginas: int = _MAX_PAGINAS_DEFECTO,
                              limit: Optional[int] = None) -> Dict[str, Any]:
        """Busca en el Sistema de Jurisprudencia de la Contraloría General de la República.

        Por defecto (``todas_las_paginas=None``) el barrido es automatico y acotado: si el universo de
        la consulta no pasa de 100 documentos (5 paginas) se trae entero; si es mayor, solo la primera
        pagina, igual que antes. Una consulta de hasta 20 documentos responde exactamente como antes.

        ``normalizar_numeros`` (activo por defecto; no aplica con ``exact``): si la consulta trae un
        numero de ley ("21180", "21.180", "Ley N° 21.180"; criterio en ``_numeros_de_ley``), se
        consulta tambien con la otra escritura y se unen los resultados por ``docId``. Orden estable:
        primero los de la consulta tal como vino, en el orden de la API; despues los nuevos de cada
        variante, y solo si mencionan la ley en su contenido (``_cita_alguna_ley``).

        ``todas_las_paginas=True`` recorre aunque el universo sea grande; ``False`` pide una sola pagina.
        El barrido avanza desde ``page`` hasta recibir menos de 20 documentos, completar el ``total``
        informado, agotar ``max_paginas`` por variante (techo 50) o reunir ``limit`` documentos. Una pagina que solo repite documentos ya
        vistos corta el barrido; si al final faltan documentos por el orden inestable del indice, se
        repiten las paginas fronterizas (``_recuperar``) y lo que siga faltando se avisa. Un error despues de la primera pagina no descarta lo reunido: queda
        en ``advertencias`` y ``barrido_completo`` en falso. ``total`` es exacto solo con barrido
        completo; si no, es el mayor ``total`` informado por la API entre las variantes. Entre paginas
        hay 1 s de pausa; ante 429/5xx/timeout se reintenta con backoff exponencial, la pausa se
        duplica y los reintentos quedan en ``reintentos`` (y en ``self.bloqueos``).
        """
        variantes = _variantes_consulta(query) if (normalizar_numeros and not exact) else [query]
        limite = limit if limit and limit > 0 else None
        automatico = todas_las_paginas is None and page == 0
        if len(variantes) == 1 and not limite:
            if not todas_las_paginas and not automatico:
                return self._buscar_pagina(query, source, page, exact, use_cache)[0]
            if automatico:
                primera = self._buscar_pagina(query, source, page, exact, use_cache)[0]
                if not _TAMANO_PAGINA < int(primera.get("total") or 0) <= _UMBRAL_BARRIDO_AUTOMATICO:
                    return primera  # cabe en una pagina o es un universo grande: como antes

        if todas_las_paginas:
            tope = max(1, min(int(max_paginas), _TECHO_PAGINAS))
        else:
            tope = -(-_UMBRAL_BARRIDO_AUTOMATICO // _TAMANO_PAGINA) if automatico else 1
        barrido = _Barrido(self, source, exact, use_cache, tope, limite,
                           _UMBRAL_BARRIDO_AUTOMATICO if automatico else None)
        if len(variantes) == 1 and automatico and not limite:
            barrido.semilla = primera  # la pagina 0 ya se pidio: no se vuelve a pedir
        return barrido.ejecutar(query, variantes, page, forzar_detalle=bool(todas_las_paginas))

    def get_dictamen(self, doc_id: str) -> Dict[str, Any]:
        """Obtiene un dictamen por su código oficial, validando el identificador.

        ``exact_search`` de la CGR no filtra: solo estrecha el análisis del texto.
        Quedarse con ``resultados[0]`` devolvía otro dictamen (pedir E311060N23
        entregaba 0E6541N25). Ante duda se retorna error con los candidatos:
        una cita falsa es peor que una búsqueda sin resultado.
        """
        buscado = doc_id.strip().upper().replace(" ", "")
        candidatos: List[Dict[str, Any]] = []
        for exact in (True, False):
            res, _ = self._buscar_pagina(buscado, "dictamenes", 0, exact, True)
            for item in res.get("resultados", []):
                if item["docId"].upper() == buscado or item.get("numero", "").upper() == buscado:
                    return item
                candidatos.append(item)
            # Fallback: <NUMERO>N<AA>, comparando número y año por separado.
            m = re.match(r"^(\d*[A-Z]?\d+)[N/-](\d{2,4})$", buscado)
            if m:
                numero, anio = m.group(1).lstrip("0"), m.group(2)[-2:]
                for item in res.get("resultados", []):
                    if item.get("numero", "").upper().lstrip("0") == numero and item.get("anio", "")[-2:] == anio:
                        return item
        return {
            "error": f"Dictamen {doc_id} no encontrado con identificador exacto en la base de la CGR.",
            "candidatos": [c["docId"] for c in candidatos[:5]],
        }

    def search_instructivos(self, query: str, page: int = 0, **opciones: Any) -> Dict[str, Any]:
        """Busca en los Instructivos y Circulares generales de la CGR (mismas opciones que
        ``search_jurisprudencia``)."""
        return self.search_jurisprudencia(query, source="instructivos", page=page, **opciones)

    def search_auditorias(self, query: str, page: int = 0, **opciones: Any) -> Dict[str, Any]:
        """Busca en los Informes Finales de Auditoría de la CGR (mismas opciones que
        ``search_jurisprudencia``)."""
        return self.search_jurisprudencia(query, source="auditoria", page=page, **opciones)


# ==============================================================================
# CLI DE CONSULTA RÁPIDA DE JURISPRUDENCIA CGR
# ==============================================================================
if __name__ == "__main__":
    import argparse
    try:
        if hasattr(sys.stdout, "reconfigure"):
            getattr(sys.stdout, "reconfigure")(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Conector Open Legal Chile — Contraloría General de la República (CGR)")
    parser.add_argument("--buscar", type=str, help="Término de búsqueda o materia (ej. 'confianza legitima' o 'compras publicas')")
    parser.add_argument("--id", type=str, help="Código de Dictamen (ej. D286N26)")
    parser.add_argument("--instructivos", type=str, help="Buscar en Instructivos de la CGR")
    parser.add_argument("--auditorias", type=str, help="Buscar en Informes de Auditoría de la CGR")
    parser.add_argument("--todas", action="store_true", help="Recorre todas las páginas (hasta --max-paginas)")
    parser.add_argument("--max-paginas", type=int, default=_MAX_PAGINAS_DEFECTO, help="Tope de páginas por variante con --todas")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de documentos a reunir")
    args = parser.parse_args()
    opciones = {"todas_las_paginas": args.todas, "max_paginas": args.max_paginas, "limit": args.limit}

    client = CGRClient()

    if args.id:
        print(f"\n🏛️ Consultando Dictamen CGR N° {args.id}...")
        data = client.get_dictamen(args.id)
        if data and isinstance(data, dict):
            fecha = data.get('fecha', '')
            anio = fecha[:4] if fecha else 's/f'
            print(f"\n[Dictamen CGR N° {data.get('docId')} ({anio})]")
            print(f"📌 Materia / Criterio:\n{data.get('materia')}")
            if data.get("texto"):
                print(f"\n📜 Texto:\n{str(data.get('texto'))[:500]}...")
        else:
            print("❌ Dictamen no encontrado o error en respuesta.")
    elif args.instructivos:
        print(f"\n📜 Buscando Instructivos CGR: '{args.instructivos}'...")
        res = client.search_instructivos(args.instructivos, **opciones)
        print(f"Total encontrados: {res.get('total')}")
        for idx, item in enumerate(res.get("resultados", [])[:5]):
            print(f"\n[{idx+1}] Instructivo N° {item.get('docId')} ({item.get('fecha')})")
            print(f"  📌 {item.get('materia')[:200]}...")
    elif args.auditorias:
        print(f"\n🔍 Buscando Informes de Auditoría CGR: '{args.auditorias}'...")
        res = client.search_auditorias(args.auditorias, **opciones)
        print(f"Total encontrados: {res.get('total')}")
        for idx, item in enumerate(res.get("resultados", [])[:5]):
            print(f"\n[{idx+1}] Informe N° {item.get('docId')} ({item.get('fecha')})")
            print(f"  📌 {item.get('materia')[:200]}...")
    elif args.buscar or len(sys.argv) == 1:
        q = args.buscar or "compras publicas"
        print(f"\n🏛️ Buscando Dictámenes en la Contraloría: '{q}'...")
        res = client.search_jurisprudencia(q, **opciones)
        print(f"Total Dictámenes encontrados: {res.get('total')} (reunidos: {len(res.get('resultados', []))})")
        if res.get("variantes"):
            print(f"Variantes consultadas: {res['variantes']}")
        for idx, item in enumerate(res.get("resultados", [])[:5]):
            print(f"\n[{idx+1}] Dictamen CGR N° {item.get('docId')} ({item.get('fecha')})")
            print(f"  📌 Materia: {item.get('materia')}")
