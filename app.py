import streamlit as st
import img2pdf
import json
import os
import re
from google import genai
from google.genai import types
from pyzotero import zotero

st.set_page_config(page_title="Avis til Zotero", page_icon="📰", layout="centered")
st.title("📰 Avisutklipp til Zotero")

# Hent konfigurasjon fra Streamlit Secrets
ZOTERO_USER_ID = str(st.secrets["5646960"])
ZOTERO_API_KEY = str(st.secrets["OVBzguFpLhZstCm95xpyo9Qw "])
GEMINI_API_KEY = str(st.secrets["AQ.Ab8RN6JV_Dd4hJGam5PLodyMGP-Khxv5KWvBjKnXvKkxEEQ9nQ"])

opplastede_filer = st.file_uploader(
    "Dra inn utklippene av oppslaget (første bilde må inneholde tittel/byline)",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True
)

if opplastede_filer:
    # Sorter filene alfabetisk på navn (sak_1, sak_2 etc.)
    opplastede_filer.sort(key=lambda x: x.name)
    st.caption(f"Filer i rekkefølge: {', '.join([f.name for f in opplastede_filer])}")

    if st.button("🚀 Behandle og send til Zotero", type="primary"):
        progress = st.progress(0, text="Analyserer oppslaget med Gemini...")
        
        try:
            # 1. Hent metadata fra bilde 1 med Gemini Flash
            forste_bilde_bytes = opplastede_filer[0].getvalue()
            client = genai.Client(api_key=GEMINI_API_KEY)
            
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

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    types.Part.from_bytes(data=forste_bilde_bytes, mime_type='image/png'),
                    prompt
                ]
            )

            # Rens svartekst for eventuelle kodeblokker
            renset_json = re.sub(r"^```json\s*|\s*```$", "", response.text.strip(), flags=re.MULTILINE)
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
