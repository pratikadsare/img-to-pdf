# Bulk Image and PDF Tool

A Streamlit app to download images in bulk from URLs, use the best available filename, auto-convert IMG naming to SDS, and export either images or PDFs.

## Features

- Paste image URLs directly
- Upload TXT or CSV containing URLs
- Try to use original filename from server response
- Fallback to CDN or URL filename
- Auto convert IMG naming to SDS naming for PXM
- Output as images only, one PDF per image, or one merged PDF
- A4, Letter, or Original image size page setup
- ZIP download with report CSV

## Run locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Deploy on Streamlit

1. Push these files to GitHub
2. Open Streamlit Community Cloud
3. Choose your repo
4. Set main file as `streamlit_app.py`
5. Deploy
