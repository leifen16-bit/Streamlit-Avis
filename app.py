import streamlit as st
import img2pdf
import json
import os
import re
import base64
import time
import io
import requests
from PIL import Image
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from pyzotero import zotero

st.set_page_config(page_title="Avis til Zotero", page_icon="📰", layout="centered")

if "opplastings_id" not in st.session_state:
    st.session_state.opplastings_id = 0

def neste_artikkel():
    st.session_state.opplastings_id += 1
    st.rerun()

col_tittel, col_nullstill = st.columns([4, 1])
with col_tittel:
    st.title("📰 Avisutklipp til Zotero")
with col_nullstill:
    st.write("")
    if st.button("🔄 Tøm felt", help="Nullstill URL og opplastede filer"):
        neste_artikkel()

MAPPE_NAVN = "Avisartikler via Streamlit"

# Hent konfigurasjon fra Streamlit Secrets
ZOTERO_USER_ID = str(st.secrets["ZOTERO_USER_ID"]).strip().strip('"').strip("'")
ZOTERO_API_KEY = str(st.secrets["ZOTERO_API_KEY"]).strip().strip('"').strip("'")
GEMINI_API_KEY = str(st.secrets["GEMINI_API_KEY"]).strip().strip('"').strip("'")

nb_url_input = st.text_input(
    "🔗 Valgfri URL til Nasjonalbiblioteket / kilde (kan stå tom):",
    key=f"nb_url_{st.session_state.opplastings_id}"
)

opplastede_filer = st.file_uploader(
    "Dra inn utklippene av oppslaget (første bilde må inneholde tittel/byline)",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.opplastings_id}"
)

# Standard aktiv: Deler automatisk dobbeltsider i to stående sider
auto_splitt = st.checkbox("📖 Del dobbeltoppslag automatisk til stående enkeltsider (venstre/høyre)", value=True)

def rens_nb_url(url_tekst):
    """Renser Nasjonalbiblioteket-lenker for søkeord og støy, men beholder sidetall."""
    if not url_tekst or not url_tekst.strip():
        return ""
    url_tekst = url_tekst.strip()
    parsed = urlparse(url_tekst)
    
    if "nb.no" in parsed.netloc:
        query_params = parse_qs(parsed.query)
        ny_query = {}
        if "page" in query_params:
            ny_query["page"] = query_params["page"][0]
        
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode(ny_query) if ny_query else "",
            ""
        ))
    return url_tekst

def finn_eller_opprett_samling(zot, samlingsnavn):
    """Finner Zotero-nøkkelen til en samling/mappe uansett hvor den ligger i biblioteket."""
    alle_samlinger = zot.collections()
    for col in alle_samlinger:
        if col.get("data", {}).get("name") == samlingsnavn:
            return col["key"]
    
    ny_samling = zot.create_collections([{"name": samlingsnavn, "parentCollection": False}])
    return ny_samling["successful"]["0"]["key"]

def prosesser_og_splitt_sider(filer, aktiver_splitt=True):
    """
    Går gjennom opplastede bildefiler i rekkefølge.
    Hvis et bilde er bredere enn det er høyt (dobbeltside), deles det automatisk
    på midten til to separate stående sider (venstre side først, deretter høyre).
    """
    ferdig_sider_bytes = []
    
    for fil in filer:
        raw = fil.getvalue()
        if not aktiver_splitt:
            ferdig_sider_bytes.append(raw)
            continue
            
        try:
            bilde = Image.open(io.BytesIO(raw))
            b, h = bilde.size
            
            # Hvis bildet er liggende (dobbeltoppslag fra avisleser)
            if b > h:
                midtpunkt = b // 2
                venstre_halvdel = bilde.crop((0, 0, midtpunkt, h))
                hoyre_halvdel = bilde.crop((midtpunkt, 0, b, h))
                
                for del_bilde in [venstre_halvdel, hoyre_halvdel]:
                    if del_bilde.mode in ("RGBA", "P"):
                        del_bilde = del_bilde.convert("RGB")
                    buf = io.BytesIO()
                    del_bilde.save(buf, format="JPEG", quality=95)
                    ferdig_sider_bytes.append(buf.getvalue())
            else:
                ferdig_sider_bytes.append(raw)
        except Exception:
            ferdig_sider_bytes.append(raw)
            
    return ferdig_sider_bytes

if opplastede_filer:
    opplastede_filer.sort(key=lambda x: x.name)
    st.caption(f"Filer i rekkefølge: {', '.join([f.name for f in opplastede_filer])}")

    if st.button("🚀 Behandle og send til Zotero", type="primary"):
        progress = st.progress(0, text="Klargjør sider og deler eventuelle dobbeltoppslag...")
        
        try:
            # 1. Splitt oppslag til enkeltsider
            enkeltsider_bytes = prosesser_og_splitt_sider(opplastede_filer, aktiver_splitt=auto_splitt)
            progress.progress(20, text=f"Genererte {len(enkeltsider_bytes)} stående sider. Analyserer med Gemini...")

            # 2. Klargjør enkeltsidene for Gemini
            deler_til_gemini = []
            for side_bytes in enkeltsider_bytes:
                b64_bilde = base64.b64encode(side_bytes).decode("utf-8")
                deler_til_gemini.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": b64_bilde
                    }
                })

            prompt = """
            Analyser disse avissidene (alle sidene i en komplett avisartikkel, vist side for side) og trekk ut bibliografisk metadata.
            
            VIKTIG OM METADATA:
            - Les av det FAKTISKE året og datoen trykket i avishodet (f.eks. '2025-10-04' eller '2026-09-19'). Format: YYYY-MM-DD.
            - Les hele sidetallsintervallet for artikkelen basert på sidetallene i hjørnene (f.eks. '14-20').
            - Ikke bruk doble anførselstegn inni tittel eller sammendrag (bruk enkle sitattegn ' eller utelat).
            
            VIKTIG OM SAMMENDRAGET (abstractNote):
            - Skriv et substansielt, presist og faglig velskrevet sammendrag på 4-6 setninger på norsk.
            - Baser deg på HELE artikkelen (ikke bare ingressen).
            - Gjør rede for sakens kjerne, sentrale personer og sitater, vesentlige faglige eller prinsipielle argumenter, eventuelle motstemmer/kritikk i artikkelen, samt konklusjon eller nåværende status.

            VIKTIG OM EMNEORD (tags):
            - Generer 3-6 relevante tematiske emneord om sakens innhold.
            - EGET PERSONSØK: Undersøk nøye om navnet 'Leif Egil', 'Reve', eller 'Leif Egil Rønaasen Reve' er nevnt noe sted på sidene (i brødtekst, sitater, byline eller bildetekster). Dersom dette navnet forekommer, SKAL taggen 'Leif Egil Reve' ALLTID legges til i 'tags'-listen i tillegg til de andre emneordene.

            Returner et JSON-objekt med nøyaktig disse feltene:
            {
              "title": "Hovedoverskrift på artikkelen",
              "authors": [{"firstName": "Fornavn", "lastName": "Etternavn"}],
              "publicationTitle": "Navn på avisen",
              "place": "By/sted",
              "section": "Seksjon (f.eks. Helg, Magasin, Nyheter, Hovedsaken)",
              "date": "YYYY-MM-DD",
              "pages": "Sidetall/sideintervall (f.eks. 14-20)",
              "language": "Norsk",
              "tags": ["3-6 emneord", "pluss ev. 'Leif Egil Reve'"],
              "abstractNote": "Substansielt sammendrag på 4-6 setninger som dekker hele saken"
            }
            """
            deler_til_gemini.append({"text": prompt})

            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY
            }
            payload = {
                "contents": [{"parts": deler_til_gemini}],
                "generationConfig": {
                    "response_mime_type": "application/json"
                }
            }

            resp = None
            for forsok in range(3):
                resp = requests.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    break
                elif resp.status_code == 503 and forsok < 2:
                    time.sleep(2 * (forsok + 1))
                else:
                    raise Exception(f"Gemini API feil ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            raw_text = result_json["candidates"][0]["content"]["parts"][0]["text"]
            metadata = json.loads(raw_text.strip())

            usage = result_json.get("usageMetadata", {})
            totalt_tokens = usage.get("totalTokenCount", 0)

            progress.progress(60, text=f"Fant: «{metadata.get('title')}» ({metadata.get('pages')}). Pakker PDF...")

            # 3. Pakk de enkelte sidene til en stående, tapsfri PDF
            pdf_bytes = img2pdf.convert(enkeltsider_bytes)

            filnavn_tittel = re.sub(r'[^a-zA-Z0-9æøåÆØÅ_ -]', '', metadata.get('title', 'Avisartikkel'))[:40].strip()
            temp_pdf_sti = f"/tmp/{filnavn_tittel}.pdf"
            with open(temp_pdf_sti, "wb") as f:
                f.write(pdf_bytes)

            progress.progress(80, text=f"Sender til mappen «{MAPPE_NAVN}» i Zotero...")

            # 4. Zotero-opprettelse
            zot = zotero.Zotero(ZOTERO_USER_ID, 'user', ZOTERO_API_KEY)
            samling_nokkel = finn_eller_opprett_samling(zot, MAPPE_NAVN)

            item = zot.item_template('newspaperArticle')
            item['title'] = metadata.get('title', 'Uten tittel')
            item['publicationTitle'] = metadata.get('publicationTitle', '')
            item['place'] = metadata.get('place', '')
            item['section'] = metadata.get('section', '')
            item['date'] = metadata.get('date', '')
            item['pages'] = metadata.get('pages', '')
            item['language'] = metadata.get('language', 'Norsk')
            item['abstractNote'] = metadata.get('abstractNote', '')
            item['collections'] = [samling_nokkel]
            
            renset_lenke = rens_nb_url(nb_url_input)
            if renset_lenke:
                item['url'] = renset_lenke

            creators = []
            for author in metadata.get('authors', []):
                creators.append({
                    'creatorType': 'author',
                    'firstName': author.get('firstName', ''),
                    'lastName': author.get('lastName', '')
                })
            if creators:
                item['creators'] = creators

            tags_unike = []
            for t in metadata.get('tags', []):
                t_str = str(t).strip()
                if t_str and t_str not in tags_unike:
                    tags_unike.append(t_str)

            if tags_unike:
                item['tags'] = [{'tag': t} for t in tags_unike]

            res = zot.create_items([item])
            item_key = res['successful']['0']['key']

            # Fest PDF-en under referansen
            zot.attachment_simple([temp_pdf_sti], item_key)

            if os.path.exists(temp_pdf_sti):
                os.remove(temp_pdf_sti)

            progress.progress(100, text="Ferdig!")
            st.success(f"✅ Lagret i Zotero under **{MAPPE_NAVN}**: **{metadata.get('title')}** (s. {metadata.get('pages')})")
            
            if renset_lenke:
                st.caption(f"🔗 Kildelenke: `{renset_lenke}`")
            if totalt_tokens:
                st.caption(f"⚡ Fullført analyse av {len(enkeltsider_bytes)} stående sider på {totalt_tokens} tokens.")

            with st.expander("Se registrerte metadata, sammendrag og emneord"):
                st.json(metadata)

            st.divider()
            st.button("✨ Klargjør for neste artikkel", on_click=neste_artikkel, type="primary")

        except Exception as e:
            st.error(f"Det oppstod en feil: {e}")
