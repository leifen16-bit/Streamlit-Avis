import streamlit as st
import img2pdf
import json
import os
import re
import base64
import time
import requests
from pyzotero import zotero

st.set_page_config(page_title="Avis til Zotero", page_icon="📰", layout="centered")
st.title("📰 Avisutklipp til Zotero")

MAPPE_NAVN = "Avisartikler via Streamlit"

# Hent konfigurasjon fra Streamlit Secrets
ZOTERO_USER_ID = str(st.secrets["ZOTERO_USER_ID"]).strip().strip('"').strip("'")
ZOTERO_API_KEY = str(st.secrets["ZOTERO_API_KEY"]).strip().strip('"').strip("'")
GEMINI_API_KEY = str(st.secrets["GEMINI_API_KEY"]).strip().strip('"').strip("'")

nb_url = st.text_input("🔗 Valgfri URL til Nasjonalbiblioteket / kilde (kan stå tom):")

opplastede_filer = st.file_uploader(
    "Dra inn utklippene av oppslaget (første bilde må inneholde tittel/byline)",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True
)

def finn_eller_opprett_samling(zot, samlingsnavn):
    """Finner Zotero-nøkkelen til en samling/mappe uansett hvor den ligger i biblioteket."""
    alle_samlinger = zot.collections()
    for col in alle_samlinger:
        if col.get("data", {}).get("name") == samlingsnavn:
            return col["key"]
    
    ny_samling = zot.create_collections([{"name": samlingsnavn, "parentCollection": False}])
    return ny_samling["successful"]["0"]["key"]

if opplastede_filer:
    opplastede_filer.sort(key=lambda x: x.name)
    st.caption(f"Filer i rekkefølge: {', '.join([f.name for f in opplastede_filer])}")

    if st.button("🚀 Behandle og send til Zotero", type="primary"):
        progress = st.progress(0, text=f"Sender alle {len(opplastede_filer)} sider til full analyse...")
        
        try:
            # 1. Klargjør samtlige sider for Gemini
            deler_til_gemini = []
            for fil in opplastede_filer:
                b64_bilde = base64.b64encode(fil.getvalue()).decode("utf-8")
                deler_til_gemini.append({
                    "inline_data": {
                        "mime_type": fil.type or "image/png",
                        "data": b64_bilde
                    }
                })

            prompt = """
            Analyser disse avissidene (alle sidene i en komplett avisartikkel) og trekk ut bibliografisk metadata.
            
            VIKTIG OM METADATA:
            - Les av det FAKTISKE året og datoen trykket i avishodet (f.eks. '2026-09-19'). Format: YYYY-MM-DD.
            - Les hele sidetallsintervallet for artikkelen (f.eks. '16-21').
            - Ikke bruk doble anførselstegn inni tittel eller sammendrag (bruk enkle sitattegn ' eller utelat).
            
            VIKTIG OM SAMMENDRAGET (abstractNote):
            - Skriv et substansielt, presist og faglig velskrevet sammendrag på 4-6 setninger på norsk.
            - Baser deg på HELE artikkelen (ikke bare en kopi av ingressen).
            - Gjør rede for sakens kjerne, sentrale personer og sitater, vesentlige faglige eller prinsipielle argumenter, eventuelle motstemmer/kritikk i artikkelen, samt konklusjon eller nåværende status.

            Returner et JSON-objekt med nøyaktig disse feltene:
            {
              "title": "Hovedoverskrift på artikkelen",
              "authors": [{"firstName": "Fornavn", "lastName": "Etternavn"}],
              "publicationTitle": "Navn på avisen",
              "place": "By/sted",
              "section": "Seksjon (f.eks. Helg, Magasin, Nyheter)",
              "date": "YYYY-MM-DD",
              "pages": "Sidetall/sideintervall (f.eks. 16-21)",
              "language": "Norsk",
              "tags": ["3-6", "relevante", "emneord"],
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

            # Token-statistikk
            usage = result_json.get("usageMetadata", {})
            totalt_tokens = usage.get("totalTokenCount", 0)

            progress.progress(45, text=f"Leste {len(opplastede_filer)} sider! Pakker tapsfri PDF...")

            # 2. Pakk alle bildene til én tapsfri PDF
            alle_bytes = [f.getvalue() for f in opplastede_filer]
            pdf_bytes = img2pdf.convert(alle_bytes)

            filnavn_tittel = re.sub(r'[^a-zA-Z0-9æøåÆØÅ_ -]', '', metadata.get('title', 'Avisartikkel'))[:40].strip()
            temp_pdf_sti = f"/tmp/{filnavn_tittel}.pdf"
            with open(temp_pdf_sti, "wb") as f:
                f.write(pdf_bytes)

            progress.progress(75, text=f"Sender til mappen «{MAPPE_NAVN}» i Zotero...")

            # 3. Zotero-opprettelse
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
            
            if nb_url.strip():
                item['url'] = nb_url.strip()

            creators = []
            for author in metadata.get('authors', []):
                creators.append({
                    'creatorType': 'author',
                    'firstName': author.get('firstName', ''),
                    'lastName': author.get('lastName', '')
                })
            if creators:
                item['creators'] = creators

            if metadata.get('tags'):
                item['tags'] = [{'tag': str(t).strip()} for t in metadata['tags']]

            res = zot.create_items([item])
            item_key = res['successful']['0']['key']

            # Fest PDF-en under referansen
            zot.attachment_simple([temp_pdf_sti], item_key)

            if os.path.exists(temp_pdf_sti):
                os.remove(temp_pdf_sti)

            progress.progress(100, text="Ferdig!")
            st.success(f"✅ Lagret i Zotero under **{MAPPE_NAVN}**: **{metadata.get('title')}** ({metadata.get('pages')})")
            
            if totalt_tokens:
                st.caption(f"⚡ Fullført analyse av {len(opplastede_filer)} sider på {totalt_tokens} tokens.")

            with st.expander("Se registrerte metadata, sammendrag og emneord"):
                st.json(metadata)

        except Exception as e:
            st.error(f"Det oppstod en feil: {e}")
