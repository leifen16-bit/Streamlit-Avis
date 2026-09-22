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

if opplastede_filer:
    opplastede_filer.sort(key=lambda x: x.name)
    st.caption(f"Filer i rekkefølge: {', '.join([f.name for f in opplastede_filer])}")

    if st.button("🚀 Behandle og send til Zotero", type="primary"):
        progress = st.progress(0, text="Analyserer oppslaget med Gemini...")
        
        try:
            # 1. Klargjør bilder for metadata (første side for byline/tittel, siste side for slutt-sidetall)
            deler_til_gemini = []
            
            # Første bilde
            b64_forste = base64.b64encode(opplastede_filer[0].getvalue()).decode("utf-8")
            deler_til_gemini.append({
                "inline_data": {
                    "mime_type": opplastede_filer[0].type or "image/png",
                    "data": b64_forste
                }
            })
            
            # Siste bilde (hvis det er mer enn 1 bilde)
            if len(opplastede_filer) > 1:
                b64_siste = base64.b64encode(opplastede_filer[-1].getvalue()).decode("utf-8")
                deler_til_gemini.append({
                    "inline_data": {
                        "mime_type": opplastede_filer[-1].type or "image/png",
                        "data": b64_siste
                    }
                })

            prompt = """
            Analyser disse avissidene (første og siste side av en artikkel) og trekk ut bibliografisk metadata.
            Svar KUN med et gyldig JSON-objekt:
            {
              "title": "Hovedoverskriften på artikkelen",
              "authors": [{"firstName": "Fornavn", "lastName": "Etternavn"}],
              "publicationTitle": "Navn på avisen (f.eks. Fædrelandsvennen)",
              "place": "By/sted avisen utgis i (f.eks. Kristiansand)",
              "section": "Seksjon/del (f.eks. Helg, Magasin, Nyheter)",
              "date": "YYYY-MM-DD",
              "pages": "Komplett sideintervall (f.eks. 16-21 basert på sidene som vises)",
              "language": "Norsk",
              "tags": ["3-5", "relevante", "emneord", "om", "saken"],
              "abstractNote": "Kort og konsist sammendrag av ingressen/saken på 1-2 setninger"
            }
            Dersom forfatter/byline mangler, la authors være tom liste.
            """
            deler_til_gemini.append({"text": prompt})

            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY
            }
            payload = {"contents": [{"parts": deler_til_gemini}]}

            # Forespørsel med retry ved midlertidig 503-kø
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
            
            renset_json = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            metadata = json.loads(renset_json)

            progress.progress(40, text=f"Fant: «{metadata.get('title')}» ({metadata.get('pages')}). Pakker PDF...")

            # 2. Pakk alle bildene til én tapsfri PDF
            alle_bytes = [f.getvalue() for f in opplastede_filer]
            pdf_bytes = img2pdf.convert(alle_bytes)

            filnavn_tittel = re.sub(r'[^a-zA-Z0-9æøåÆØÅ_ -]', '', metadata.get('title', 'Avisartikkel'))[:40].strip()
            temp_pdf_sti = f"/tmp/{filnavn_tittel}.pdf"
            with open(temp_pdf_sti, "wb") as f:
                f.write(pdf_bytes)

            progress.progress(70, text="Laster opp til Zotero Cloud...")

            # 3. Opprett element og fyll ut felter i Zotero
            zot = zotero.Zotero(ZOTERO_USER_ID, 'user', ZOTERO_API_KEY)
            item = zot.item_template('newspaperArticle')
            
            item['title'] = metadata.get('title', 'Uten tittel')
            item['publicationTitle'] = metadata.get('publicationTitle', '')
            item['place'] = metadata.get('place', '')
            item['section'] = metadata.get('section', '')
            item['date'] = metadata.get('date', '')
            item['pages'] = metadata.get('pages', '')
            item['language'] = metadata.get('language', 'Norsk')
            item['abstractNote'] = metadata.get('abstractNote', '')
            
            if nb_url.strip():
                item['url'] = nb_url.strip()

            # Legg til forfattere
            creators = []
            for author in metadata.get('authors', []):
                creators.append({
                    'creatorType': 'author',
                    'firstName': author.get('firstName', ''),
                    'lastName': author.get('lastName', '')
                })
            if creators:
                item['creators'] = creators

            # Legg til emneord (tags)
            if metadata.get('tags'):
                item['tags'] = [{'tag': str(t).strip()} for t in metadata['tags']]

            # Opprett referansen i Zotero
            res = zot.create_items([item])
            item_key = res['successful']['0']['key']

            # Fest PDF-en som vedlegg under referansen
            zot.attachment_simple([temp_pdf_sti], item_key)

            if os.path.exists(temp_pdf_sti):
                os.remove(temp_pdf_sti)

            progress.progress(100, text="Ferdig!")
            st.success(f"✅ Lagret i Zotero: **{metadata.get('title')}** ({metadata.get('pages')})")
            
            with st.expander("Se registrerte metadata og emneord"):
                st.json(metadata)

        except Exception as e:
            st.error(f"Det oppstod en feil: {e}")
