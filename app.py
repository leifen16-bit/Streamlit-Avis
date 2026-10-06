import streamlit as st
import img2pdf
import json
import os
import re
import time
import io
from PIL import Image
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from pyzotero import zotero

from google import genai
from google.genai import types
from google.oauth2 import service_account

st.set_page_config(page_title="Avis til Zotero", page_icon="📰", layout="centered")

# Standardregler for systematiske emneord (kan redigeres direkte i appen)
STANDARD_TAGG_REGLER = """Leif Egil Reve: Nevner 'Leif Egil', 'Reve' eller 'Leif Egil Rønaasen Reve' i brødtekst, byline eller bildetekster
Kommunikasjon og livssyn: Nevner 'Kommunikasjon og livssyn', forkortelsen 'KL' i studiesammenheng, eller fagfeltet
Artikkel-KL: Nevner 'Kommunikasjon og livssyn' eller 'KL'"""

# Håndter nullstilling av opplastingsfelter
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

# Autentisering og oppsett mot Vertex AI
@st.cache_resource
def get_vertex_client():
    gcp_info = dict(st.secrets["gcp_service_account"])
    project_id = gcp_info.get("project_id", "project-5aad088e-3f07-49db-be9")
    creds = service_account.Credentials.from_service_account_info(
        gcp_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return genai.Client(
        vertexai=True,
        project=project_id,
        location="global",
        credentials=creds,
    )

try:
    ai_client = get_vertex_client()
except Exception as e:
    st.error(f"Kunne ikke koble til Vertex AI. Sjekk [gcp_service_account] i Streamlit Secrets: {e}")
    st.stop()

# Zotero konfigurasjon
ZOTERO_USER_ID = str(st.secrets["ZOTERO_USER_ID"]).strip().strip('"').strip("'")
ZOTERO_API_KEY = str(st.secrets["ZOTERO_API_KEY"]).strip().strip('"').strip("'")

# Sidebar: Modellvalg
valgt_modell = st.sidebar.selectbox(
    "🤖 Gemini-modell (Vertex AI)",
    ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"],
    index=0
)

# Input-felter
nb_url_input = st.text_input(
    "🔗 Valgfri URL til Nasjonalbiblioteket / kilde (kan stå tom):",
    key=f"nb_url_{st.session_state.opplastings_id}"
)

# Egendefinerte tagg-regler som kan tilpasses direkte i UI
with st.expander("🏷️ Faste sporings- og taggregler (klikk for å tilpasse)", expanded=False):
    st.caption("Skriv én regel per linje: `Taggnavn: Søkeord eller kriterier`. Gemini sjekker teksten for disse i tillegg til dynamiske emneord.")
    aktive_tagg_regler = st.text_area(
        "Aktive regler:",
        value=STANDARD_TAGG_REGLER,
        height=120,
        key="custom_tag_rules"
    )

opplastede_filer = st.file_uploader(
    "Dra inn utklippene av oppslaget (første bilde må inneholde tittel/byline)",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.opplastings_id}"
)

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
    """Deler liggende oppslag i to stående enkeltsider."""
    ferdig_sider_bytes = []
    for fil in filer:
        raw = fil.getvalue()
        if not aktiver_splitt:
            ferdig_sider_bytes.append(raw)
            continue
            
        try:
            bilde = Image.open(io.BytesIO(raw))
            b, h = bilde.size
            
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

def generer_regel_instruks(regler_tekst):
    """Bygger prompt-instruksjoner basert på brukerens definerte tagg-regler."""
    linjer = [l.strip() for l in regler_tekst.strip().split("\n") if l.strip() and not l.startswith("#")]
    if not linjer:
        return ""
    
    instruks = "\nVIKTIG OM SPESIFIKKE FASTE EMNEORD (tags):\n"
    instruks += "I tillegg til generelle emneord, skal du kontrollere følgende faste regler mot hele teksten:\n"
    for l in linjer:
        if ":" in l:
            tagg, kriterie = l.split(":", 1)
            instruks += f"- Hvis teksten oppfyller '{kriterie.strip()}', SKAL taggen '{tagg.strip()}' inkluderes i 'tags'-listen.\n"
        else:
            instruks += f"- Hvis teksten nevner '{l.strip()}', SKAL taggen '{l.strip()}' inkluderes i 'tags'-listen.\n"
    return instruks

if opplastede_filer:
    opplastede_filer.sort(key=lambda x: x.name)
    st.caption(f"Filer i rekkefølge: {', '.join([f.name for f in opplastede_filer])}")

    if st.button("🚀 Behandle og send til Zotero", type="primary"):
        progress = st.progress(0, text="Klargjør sider og deler eventuelle dobbeltoppslag...")
        
        try:
            # 1. Splitt oppslag til enkeltsider
            enkeltsider_bytes = prosesser_og_splitt_sider(opplastede_filer, aktiver_splitt=auto_splitt)
            progress.progress(20, text=f"Genererte {len(enkeltsider_bytes)} stående sider. Analyserer med {valgt_modell} via Vertex AI...")

            # 2. Klargjør enkeltsidene for Vertex AI GenAI SDK
            innhold_til_gemini = []
            for side_bytes in enkeltsider_bytes:
                innhold_til_gemini.append(
                    types.Part.from_bytes(
                        data=side_bytes,
                        mime_type="image/jpeg",
                    )
                )

            systematiske_instrukser = generer_regel_instruks(aktive_tagg_regler)

            prompt = f"""
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
            - Generer 3-6 relevante dynamiske emneord om sakens faglige/tematiske kjerne.
            {systematiske_instrukser}

            Returner et JSON-objekt med nøyaktig disse feltene:
            {{
              "title": "Hovedoverskrift på artikkelen",
              "authors": [{{"firstName": "Fornavn", "lastName": "Etternavn"}}],
              "publicationTitle": "Navn på avisen",
              "place": "By/sted",
              "section": "Seksjon (f.eks. Helg, Magasin, Nyheter, Hovedsaken)",
              "date": "YYYY-MM-DD",
              "pages": "Sidetall/sideintervall (f.eks. 14-20)",
              "language": "Norsk",
              "tags": ["3-6 generelle emneord", "pluss eventuelle faste tagger utløst av reglene"],
              "abstractNote": "Substansielt sammendrag på 4-6 setninger som dekker hele saken"
            }}
            """
            innhold_til_gemini.append(prompt)

            # 3. Kjør Vertex AI-kall med automatisk retry ved nettverksavbrudd
            config = types.GenerateContentConfig(
                response_mime_type="application/json"
            )

            response = None
            for forsok in range(3):
                try:
                    response = ai_client.models.generate_content(
                        model=valgt_modell,
                        contents=innhold_til_gemini,
                        config=config,
                    )
                    break
                except Exception as e:
                    if forsok < 2:
                        time.sleep(2 * (forsok + 1))
                    else:
                        raise e

            raw_text = response.text
            renset_json = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            metadata = json.loads(renset_json)

            # Tokenstatistikk fra Vertex AI
            totalt_tokens = 0
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                totalt_tokens = getattr(response.usage_metadata, "total_token_count", 0)

            progress.progress(60, text=f"Fant: «{metadata.get('title')}» ({metadata.get('pages')}). Pakker PDF...")

            # 4. Pakk de enkelte sidene til en stående, tapsfri PDF
            pdf_bytes = img2pdf.convert(enkeltsider_bytes)

            filnavn_tittel = re.sub(r'[^a-zA-Z0-9æøåÆØÅ_ -]', '', metadata.get('title', 'Avisartikkel'))[:40].strip()
            temp_pdf_sti = f"/tmp/{filnavn_tittel}.pdf"
            with open(temp_pdf_sti, "wb") as f:
                f.write(pdf_bytes)

            progress.progress(80, text=f"Sender til mappen «{MAPPE_NAVN}» i Zotero...")

            # 5. Zotero-opprettelse
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

            # Sikre unike emneord
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
                st.caption(f"⚡ Fullført analyse via Vertex AI på {totalt_tokens} tokens.")

            with st.expander("Se registrerte metadata, sammendrag og emneord"):
                st.json(metadata)

            st.divider()
            st.button("✨ Klargjør for neste artikkel", on_click=neste_artikkel, type="primary")

        except Exception as e:
            st.error(f"Det oppstod en feil: {e}")
