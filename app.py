import streamlit as st
import img2pdf
import json
import os
import re
import base64
import requests
from pyzotero import zotero

st.set_page_config(page_title="Avis til Zotero", page_icon="📰", layout="centered")
st.title("📰 Avisutklipp til Zotero")

# Hent konfigurasjon fra Streamlit Secrets
ZOTERO_USER_ID = str(st.secrets["ZOTERO_USER_ID"]).strip()
ZOTERO_API_KEY = str(st.secrets["ZOTERO_API_KEY"]).strip()
GEMINI_API_KEY = str(st.secrets["GEMINI_API_KEY"]).strip()

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
            # 1. Hent metadata fra bilde 1 via direkte REST-kall
            forste_bilde_bytes = opplastede_filer[0].getvalue()
            b64_image = base64.b64encode(forste_bilde_bytes).decode("utf-8")
            
            prompt = """
            Analyser denne avissiden og trekk ut bibliografisk metadata for hovedartikkelen.
            Svar KUN med et gyldig JSON-objekt:
            {
              "title": "Hovedoverskriften",
              "authors": [{"firstName": "Fornavn", "lastName": "Etternavn"}],
              "publicationTitle": "Navn på avisen",
              "date": "YYYY-MM-DD",
              "pages": "f.eks. 16-21 eller sidetall",
              "abstractNote": "Kort sammendrag av ingressen på 1-2 setninger"
            }
            Dersom forfatter/byline mangler, la authors være tom liste.
            """

            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": opplastede_filer[0].type or "image/png",
                                "data": b64_image
                            }
                        },
                        {"text": prompt}
                    ]
                }]
            }

            resp = requests.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise Exception(f"Gemini API feil ({resp.status_code}): {resp.text}")

            result_json = resp.json()
            raw_text = result_json["candidates"][0]["content"]["parts"][0]["text"]
            
            # Rens eventuelle kodeblokker rundt JSON
            renset_json = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            metadata = json.loads(renset_json)

            progress.progress(40, text=f"Fant: «{metadata.get('title')}». Pakker PDF...")

            # 2. Pakk alle bildene til en tapsfri PDF
            alle_bytes = [f.getvalue() for f in opplastede_filer]
            pdf_bytes = img2pdf.convert(alle_bytes)

            filnavn_tittel = re.sub(r'[^a-zA-Z0-9æøåÆØÅ_ -]', '', metadata.get('title', 'Avisartikkel'))[:40].strip()
            temp_pdf_sti = f"/tmp/{filnavn_tittel}.pdf"
            with open(temp_pdf_sti, "wb") as f:
                f.write(pdf_bytes)

            progress.progress(70, text="Laster opp til Zotero Cloud...")

            # 3. Opprett element og last opp PDF i Zotero
            zot = zotero.Zotero(ZOTERO_USER_ID, 'user', ZOTERO_API_KEY)
            item = zot.item_template('newspaperArticle')
            
            item['title'] = metadata.get('title', 'Uten tittel')
            item['publicationTitle'] = metadata.get('publicationTitle', '')
            item['date'] = metadata.get('date', '')
            item['pages'] = metadata.get('pages', '')
            item['abstractNote'] = metadata.get('abstractNote', '')

            creators = []
            for author in metadata.get('authors', []):
                creators.append({
                    'creatorType': 'author',
                    'firstName': author.get('firstName', ''),
                    'lastName': author.get('lastName', '')
                })
            if creators:
                item['creators'] = creators

            # Opprett referansen
            res = zot.create_items([item])
            item_key = res['successful']['0']['key']

            # Fest PDF-filen til referansen
            zot.attachment_simple([temp_pdf_sti], item_key)

            if os.path.exists(temp_pdf_sti):
                os.remove(temp_pdf_sti)

            progress.progress(100, text="Ferdig!")
            st.success(f"✅ Lagret i Zotero: **{metadata.get('title')}** ({metadata.get('publicationTitle')})")
            
            with st.expander("Se registrerte metadata"):
                st.json(metadata)

        except Exception as e:
            st.error(f"Det oppstod en feil: {e}")
