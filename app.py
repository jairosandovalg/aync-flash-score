import asyncio
import os
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# --- CONFIGURACIÓN ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8923959866:AAES1dc4LAsedUKUsGR4p5D1SkaMt7nKyes")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7272170952")
CONCURRENCIA_MAXIMA = 3  # Nivel balanceado para evitar bloqueos por rate-limit


async def enviar_alerta_telegram_async(session: aiohttp.ClientSession, mensaje: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Error Telegram: {e}")
        return False


def parsear_bloque_estadisticas(soup_bloque) -> dict:
    stats = {}

    # 1. Filas métricas estándar (Posesión, xG, Pases, Faltas, etc.)
    for fila in soup_bloque.select('div[data-testid="wcl-statistics"]'):
        etiqueta_el = fila.select_one(".wcl-label_sO4bA span, [data-testid='wcl-scores-simple-text-01'], .wcl-name_2lXWg")
        if not etiqueta_el:
            continue
        nombre = etiqueta_el.get_text(strip=True)
        val_l = fila.select_one(".wcl-labelRow_42JBQ > div:first-child .wcl-value_Ywp3J")
        val_v = fila.select_one(".wcl-awayValue_smmfR .wcl-value_Ywp3J")
        stats[f"{nombre} (L)"] = val_l.get_text(strip=True) if val_l else "-"
        stats[f"{nombre} (V)"] = val_v.get_text(strip=True) if val_v else "-"

    # 2. Remates fuera y Remates a puerta (bloques de portería)
    shot_container = soup_bloque.select_one('[class*="shotOnTargetStats_"]')
    if shot_container:
        for selector in ['[class*="offTargetBar_"]', '[class*="onTargetBar_"]']:
            barra = shot_container.select_one(selector)
            if barra:
                label = barra.select_one('[class*="wcl-label_"]')
                vals = barra.select('[class*="wcl-value_"]')
                if label:
                    nombre = label.get_text(strip=True)
                    stats[f"{nombre} (L)"] = vals[0].get_text(strip=True) if len(vals) > 0 else "-"
                    stats[f"{nombre} (V)"] = vals[-1].get_text(strip=True) if len(vals) > 1 else "-"

    # 3. Badges SVG (Córneres, Tarjetas amarillas y rojas)
    for badge in soup_bloque.select('[class*="incidentValueBadge_"]'):
        svg = badge.select_one("svg")
        if not svg:
            continue
        test_id = svg.get("data-testid", "").lower()
        tipo = None
        if "corner" in test_id:
            tipo = "Córneres"
        elif "yellow-card" in test_id or "yellow" in test_id:
            tipo = "Tarjetas amarillas"
        elif "red-card" in test_id or "red" in test_id:
            tipo = "Tarjetas rojas"

        if tipo:
            spans = [s.get_text(strip=True) for s in badge.select("span") if s.get_text(strip=True)]
            stats[f"{tipo} (L)"] = spans[0] if len(spans) >= 1 else "-"
            stats[f"{tipo} (V)"] = spans[-1] if len(spans) >= 2 else "-"

    return stats


def formatear_mensaje_partido(reg: dict) -> str:
    stats = reg.get("Stats", {})
    metricas_fijas = [
        "Posesión de balón",
        "Goles esperados (xG)",
        "Remates a puerta",
        "Remates fuera",
        "Córneres",
        "Tarjetas amarillas"
    ]
    lineas = []
    procesadas = set()

    # 1. Métricas fijas con fallback a '-'
    for m in metricas_fijas:
        kl = next((k for k in stats if m.lower() in k.lower() and "(L)" in k), None)
        kv = next((k for k in stats if m.lower() in k.lower() and "(V)" in k), None)
        lineas.append(f"• <b>{m}:</b> {stats.get(kl, '-') if kl else '-'} | {stats.get(kv, '-') if kv else '-'}")
        if kl: procesadas.add(kl.replace(" (L)", ""))
        if kv: procesadas.add(kv.replace(" (V)", ""))

    # 2. Métricas adicionales presentes
    for k, v in stats.items():
        base_name = k.replace(" (L)", "").replace(" (V)", "")
        if base_name not in procesadas and not any(f.lower() in base_name.lower() for f in metricas_fijas):
            val_l = stats.get(f"{base_name} (L)", "-")
            val_v = stats.get(f"{base_name} (V)", "-")
            lineas.append(f"• <b>{base_name}:</b> {val_l} | {val_v}")
            procesadas.add(base_name)

    stats_texto = "\n\n📊 <b>Estadísticas Principales (L | V):</b>\n" + "\n".join(lineas)
    return (
        f"⚽ <b>ALERTA DE PARTIDO</b>\n\n"
        f"⚔️ <b>Partido:</b> {reg['Partido en Vivo']}\n"
        f"🔢 <b>Marcador:</b> {reg['Marcador']}\n"
        f"⏱ <b>Minuto:</b> {reg['Minuto']} ({reg['Tiempo/Estado']})\n"
        f"📈 <b>Cuotas (1X2):</b> {reg['Cuotas']}"
        f"{stats_texto}"
    )


def cumple_criterios_alerta(partido: dict) -> bool:
    estado = partido.get("Tiempo/Estado", "").upper()
    estados_excluidos = ["FINALIZADO", "FIN", "FT", "APL.", "POSTP."]
    if any(ex in estado for ex in estados_excluidos):
        return False

    marcador = partido.get("Marcador", "")
    if not marcador or marcador == "- - -":
        return False

    stats = partido.get("Stats", {})
    if not stats or len(stats) == 0:
        return False


    # Exigir que existan cuotas válidas (descarta si no se encontraron o son vacías)
    cuotas = partido.get("Cuotas", "")
    if not cuotas or cuotas == "- - -":
        return False

    return True


async def procesar_partido(context, p_div_data: dict, semaforo: asyncio.Semaphore, tg_session: aiohttp.ClientSession):
    async with semaforo:
        url_partido = f"https://www.flashscore.pe/partido/{p_div_data['id']}/"
        url_stats = f"{url_partido}#/resumen-del-partido/estadisticas-del-partido/0"
        page = None
        datos_partido = {
            "Partido en Vivo": p_div_data["nombre"],
            "Marcador": "- - -",
            "Cuotas": "- - -",
            "Tiempo/Estado": "-",
            "Minuto": "-",
            "Stats": {}
        }
        try:
            page = await context.new_page()

            # 1. Cargar vista principal (Marcador, Minuto y Cuotas)
            await page.goto(url_partido, timeout=30000, wait_until="domcontentloaded")

            try:
                await page.wait_for_selector("div.detailScore__wrapper", timeout=6000)
            except Exception:
                pass

            try:
                await page.wait_for_selector("[data-testid='wcl-oddsValue']", timeout=4000)
            except Exception:
                pass

            soup_resumen = BeautifulSoup(await page.content(), "html.parser")

            score = soup_resumen.select_one("div.detailScore__wrapper")
            if score:
                datos_partido["Marcador"] = score.get_text(separator=" ", strip=True)

            status = soup_resumen.select_one("span.fixedHeaderDuel__detailStatus")
            if status:
                datos_partido["Tiempo/Estado"] = status.get_text(strip=True)

            minuto = soup_resumen.select_one("span.eventTime")
            if minuto:
                datos_partido["Minuto"] = minuto.get_text(strip=True)

            # Extraer cuotas 1X2
            botones = soup_resumen.find_all("button", attrs={"data-analytics-bookmaker-id": "660"})
            valores_cuotas = []
            for btn in botones:
                span = btn.find("span", {"data-testid": "wcl-oddsValue"})
                if span and span.get_text(strip=True):
                    valores_cuotas.append(span.get_text(strip=True))
                if len(valores_cuotas) == 3:
                    break

            if len(valores_cuotas) >= 3:
                datos_partido["Cuotas"] = f"1: {valores_cuotas[0]} | X: {valores_cuotas[1]} | 2: {valores_cuotas[2]}"

            # 2. Navegación a ESTADÍSTICAS (intenta clic; si falla o no está visible, navega directo a la URL hash)
            tab_stats = page.locator('a[data-analytics-alias="match-statistics"], a:has-text("ESTADÍSTICAS"), button:has-text("ESTADÍSTICAS")').first
            pudo_clicar = False

            if await tab_stats.count() > 0 and await tab_stats.is_visible():
                try:
                    await tab_stats.click(timeout=3000)
                    pudo_clicar = True
                except Exception:
                    pudo_clicar = False

            if not pudo_clicar:
                await page.goto(url_stats, timeout=20000, wait_until="domcontentloaded")

            # 3. Esperar que los nodos de estadísticas aparezcan en el DOM
            try:
                await page.wait_for_selector('div[data-testid="statGroup"], div[data-testid="wcl-statistics"]', timeout=7000)
                await page.wait_for_timeout(800)
            except Exception:
                await page.wait_for_timeout(1500)

            soup_stats = BeautifulSoup(await page.content(), "html.parser")
            bloque_stats = None
            grupos = soup_stats.select('div[data-testid="statGroup"]')

            for grupo in grupos:
                cabecera = grupo.select_one('[data-testid="wcl-headerSection-text"], [class*="header"]')
                if cabecera and "principal" in cabecera.get_text(strip=True).lower():
                    bloque_stats = grupo
                    break

            if not bloque_stats:
                bloque_stats = grupos[0] if grupos else soup_stats.select_one('div[data-testid="match-history"]') or soup_stats

            if bloque_stats:
                datos_partido["Stats"] = parsear_bloque_estadisticas(bloque_stats)

            # 4. Validar y enviar alerta
            if cumple_criterios_alerta(datos_partido):
                msg = formatear_mensaje_partido(datos_partido)
                await enviar_alerta_telegram_async(tg_session, msg)
                print(f"✓ Enviado: {p_div_data['nombre']} | Stats: {len(datos_partido['Stats'])}")
            else:
                print(f"Descartado: {p_div_data['nombre']} | Marcador: {datos_partido['Marcador']}")

        except Exception as e:
            print(f"Error procesando {p_div_data['nombre']}: {e}")
        finally:
            if page:
                await page.close()


async def ejecutar_escaneo_async():
    print("Iniciando escaneo asíncrono en Flashscore...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        main = await context.new_page()

        try:
            await main.goto("https://www.flashscore.pe/", timeout=35000, wait_until="domcontentloaded")
            btn_live = "//div[contains(@class, 'filters__text') and text()='EN DIRECTO']"
            await main.wait_for_selector(btn_live, timeout=15000)
            await main.locator(btn_live).click()
            await main.wait_for_timeout(3000)

            soup = BeautifulSoup(await main.content(), "html.parser")
            partidos_divs = soup.find_all("div", id=lambda x: x and x.startswith("g_1_"))

            if not partidos_divs:
                print("No se encontraron partidos en vivo.")
                return

            partidos_data = []
            for p_div in partidos_divs:
                id_p = p_div.get('id').split('_')[-1]
                h = p_div.find("div", class_=lambda c: c and "home" in c.lower() and "participant" in c.lower())
                a = p_div.find("div", class_=lambda c: c and "away" in c.lower() and "participant" in c.lower())
                partidos_data.append({
                    "id": id_p,
                    "nombre": f"{h.get_text(strip=True) if h else 'Local'} vs {a.get_text(strip=True) if a else 'Visitante'}"
                })

            print(f"Partidos en vivo detectados: {len(partidos_data)}. Procesando con concurrencia {CONCURRENCIA_MAXIMA}...")
            await main.close()

            semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
            async with aiohttp.ClientSession() as tg_session:
                tareas = [procesar_partido(context, p_data, semaforo, tg_session) for p_data in partidos_data]
                await asyncio.gather(*tareas)

        finally:
            await browser.close()
            print("Escaneo finalizado.")


if __name__ == "__main__":
    asyncio.run(ejecutar_escaneo_async())
