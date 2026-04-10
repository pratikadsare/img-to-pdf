import csv
import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st
from PIL import Image
from reportlab.lib.pagesizes import A4, letter, landscape, portrait
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

st.set_page_config(page_title="Bulk Image PDF Tool", page_icon="📄", layout="wide")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
REQUEST_TIMEOUT = 60
MAX_WORKERS = 8


def sanitize_filename(name: str) -> str:
    name = str(name).strip().replace("\x00", "")
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "downloaded_file"


def get_extension_from_content_type(content_type: str) -> str:
    if not content_type:
        return ""
    content_type = content_type.lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
        "image/x-icon": ".ico",
    }
    return mapping.get(content_type, "")


def get_name_from_content_disposition(content_disposition: str) -> str:
    if not content_disposition:
        return ""

    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.I)
    if match:
        return sanitize_filename(unquote(match.group(1).strip().strip('"')))

    match = re.search(r'filename="?([^";]+)"?', content_disposition, flags=re.I)
    if match:
        return sanitize_filename(unquote(match.group(1).strip()))

    return ""


def get_name_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        raw_name = Path(unquote(parsed.path)).name
        return sanitize_filename(raw_name)
    except Exception:
        return ""


def ensure_extension(filename: str, content_type: str, url: str) -> str:
    current_ext = Path(filename).suffix
    if current_ext:
        return filename

    ext = get_extension_from_content_type(content_type)
    if ext:
        return filename + ext

    url_name = get_name_from_url(url)
    url_ext = Path(url_name).suffix
    if url_ext:
        return filename + url_ext

    return filename + ".jpg"


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def parse_urls_from_text(text: str) -> list[str]:
    urls = []
    for line in text.splitlines():
        value = line.strip()
        if value.startswith("http://") or value.startswith("https://"):
            urls.append(value)
    return dedupe_keep_order(urls)


def parse_urls_from_uploaded_file(uploaded_file) -> list[str]:
    raw = uploaded_file.read()
    try:
        content = raw.decode("utf-8-sig")
    except Exception:
        content = raw.decode("latin-1")

    urls = []
    if uploaded_file.name.lower().endswith(".csv"):
        reader = csv.reader(io.StringIO(content))
        for row in reader:
            for cell in row:
                value = cell.strip()
                if value.startswith("http://") or value.startswith("https://"):
                    urls.append(value)
                    break
    else:
        urls.extend(parse_urls_from_text(content))

    return dedupe_keep_order(urls)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def convert_image_name_to_sds(name: str) -> str:
    base = Path(str(name)).stem
    base = sanitize_filename(base)
    base = re.sub(r"_(ISP[_-]?\d+|ISPXX|ISP|IMG[_-]?\d*|IMAGE[_-]?\d*|IMAGE|IMG)$", "", base, flags=re.I)
    base = re.sub(r"_+", "_", base).rstrip("_")
    if not re.search(r"_SDS$", base, flags=re.I):
        base = f"{base}_SDS"
    return base


def make_unique_name(filename: str, used_names: set[str]) -> str:
    base = Path(filename).stem
    ext = Path(filename).suffix
    candidate = filename
    counter = 1
    while candidate.lower() in used_names:
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    used_names.add(candidate.lower())
    return candidate


def download_one(url: str, naming_mode: str, custom_prefix: str, auto_sds: bool, serial_index: int) -> dict:
    session = build_session()
    response = session.get(url, stream=True, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    content_disposition = response.headers.get("Content-Disposition", "")

    header_name = get_name_from_content_disposition(content_disposition)
    url_name = get_name_from_url(response.url) or get_name_from_url(url)

    if naming_mode == "Original name from server":
        chosen_name = header_name or url_name or f"image_{serial_index:03d}"
        name_source = "content-disposition" if header_name else "url"
    elif naming_mode == "CDN or URL name":
        chosen_name = url_name or header_name or f"image_{serial_index:03d}"
        name_source = "url" if url_name else "content-disposition"
    else:
        prefix = sanitize_filename(custom_prefix) or "image"
        chosen_name = f"{prefix}_{serial_index:03d}"
        name_source = "custom-prefix"

    chosen_name = sanitize_filename(Path(chosen_name).stem)
    if auto_sds:
        chosen_name = convert_image_name_to_sds(chosen_name)

    final_image_name = ensure_extension(chosen_name, content_type, response.url)

    content = io.BytesIO()
    for chunk in response.iter_content(chunk_size=1024 * 64):
        if chunk:
            content.write(chunk)
    content.seek(0)

    return {
        "url": url,
        "final_url": response.url,
        "status": "success",
        "image_name": final_image_name,
        "pdf_name": f"{Path(chosen_name).stem}.pdf",
        "name_source": name_source,
        "content_type": content_type,
        "http_status": response.status_code,
        "error": "",
        "bytes": content.getvalue(),
    }


def download_task_wrapper(url: str, naming_mode: str, custom_prefix: str, auto_sds: bool, serial_index: int) -> dict:
    try:
        return download_one(url, naming_mode, custom_prefix, auto_sds, serial_index)
    except Exception as e:
        return {
            "url": url,
            "final_url": "",
            "status": "failed",
            "image_name": "",
            "pdf_name": "",
            "name_source": "",
            "content_type": "",
            "http_status": "",
            "error": str(e),
            "bytes": b"",
        }


def run_bulk_download(urls: list[str], naming_mode: str, custom_prefix: str, auto_sds: bool) -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_task_wrapper, url, naming_mode, custom_prefix, auto_sds, idx + 1): url
            for idx, url in enumerate(urls)
        }
        progress = st.progress(0)
        status = st.empty()

        completed = 0
        total = len(futures)

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            progress.progress(completed / total)
            status.info(f"Processed {completed} of {total}")

        status.success(f"Completed {completed} of {total}")
    return results


def get_page_size(image_width: int, image_height: int, page_mode: str):
    if page_mode == "Original image size":
        return float(image_width), float(image_height)

    is_landscape = image_width > image_height
    if page_mode == "A4":
        return landscape(A4) if is_landscape else portrait(A4)
    return landscape(letter) if is_landscape else portrait(letter)


def make_pdf_from_image_bytes(image_bytes: bytes, page_mode: str, fit_mode: str, margin: int) -> bytes:
    image = Image.open(io.BytesIO(image_bytes))
    image = image.convert("RGB")
    img_w, img_h = image.size

    page_w, page_h = get_page_size(img_w, img_h, page_mode)
    usable_w = max(page_w - margin * 2, 1)
    usable_h = max(page_h - margin * 2, 1)

    img_ratio = img_w / img_h
    area_ratio = usable_w / usable_h

    if fit_mode == "Fill page":
        if img_ratio > area_ratio:
            draw_h = usable_h
            draw_w = draw_h * img_ratio
        else:
            draw_w = usable_w
            draw_h = draw_w / img_ratio
    else:
        if img_ratio > area_ratio:
            draw_w = usable_w
            draw_h = draw_w / img_ratio
        else:
            draw_h = usable_h
            draw_w = draw_h / img_ratio

    x = (page_w - draw_w) / 2
    y = (page_h - draw_h) / 2

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(page_w, page_h))
    pdf.drawImage(ImageReader(image), x, y, width=draw_w, height=draw_h, preserveAspectRatio=False, mask='auto')
    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def build_zip(results: list[dict], output_mode: str, page_mode: str, fit_mode: str, margin: int, merged_pdf_name: str) -> tuple[bytes, str]:
    used_names = set()
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        report_buffer = io.StringIO()
        writer = csv.DictWriter(
            report_buffer,
            fieldnames=["url", "final_url", "status", "image_name", "pdf_name", "name_source", "content_type", "http_status", "error"],
        )
        writer.writeheader()

        success_rows = [r for r in results if r["status"] == "success"]

        for row in results:
            row_for_csv = {k: v for k, v in row.items() if k != "bytes"}
            writer.writerow(row_for_csv)

        if output_mode == "Images only":
            for row in success_rows:
                unique_name = make_unique_name(row["image_name"], used_names)
                zf.writestr(unique_name, row["bytes"])

        elif output_mode == "One PDF per image":
            for row in success_rows:
                pdf_bytes = make_pdf_from_image_bytes(row["bytes"], page_mode, fit_mode, margin)
                unique_name = make_unique_name(row["pdf_name"], used_names)
                zf.writestr(unique_name, pdf_bytes)

        else:
            merged_buffer = io.BytesIO()
            merged_pdf = None
            for row in success_rows:
                image = Image.open(io.BytesIO(row["bytes"]))
                image = image.convert("RGB")
                img_w, img_h = image.size
                page_w, page_h = get_page_size(img_w, img_h, page_mode)
                usable_w = max(page_w - margin * 2, 1)
                usable_h = max(page_h - margin * 2, 1)
                img_ratio = img_w / img_h
                area_ratio = usable_w / usable_h

                if fit_mode == "Fill page":
                    if img_ratio > area_ratio:
                        draw_h = usable_h
                        draw_w = draw_h * img_ratio
                    else:
                        draw_w = usable_w
                        draw_h = draw_w / img_ratio
                else:
                    if img_ratio > area_ratio:
                        draw_w = usable_w
                        draw_h = draw_w / img_ratio
                    else:
                        draw_h = usable_h
                        draw_w = draw_h / img_ratio

                x = (page_w - draw_w) / 2
                y = (page_h - draw_h) / 2

                if merged_pdf is None:
                    merged_pdf = canvas.Canvas(merged_buffer, pagesize=(page_w, page_h))
                else:
                    merged_pdf.setPageSize((page_w, page_h))

                merged_pdf.drawImage(ImageReader(image), x, y, width=draw_w, height=draw_h, preserveAspectRatio=False, mask='auto')
                merged_pdf.showPage()

            if merged_pdf is not None:
                merged_pdf.save()
                merged_buffer.seek(0)
                merged_name = sanitize_filename(Path(merged_pdf_name or "merged_images").stem) + ".pdf"
                zf.writestr(merged_name, merged_buffer.getvalue())

        zf.writestr("download_report.csv", report_buffer.getvalue().encode("utf-8-sig"))

    zip_buffer.seek(0)
    return zip_buffer.getvalue(), "bulk_image_pdf_output.zip"


st.title("📄 Bulk Image and PDF Tool")
st.caption("Download images from URLs, keep the best available filename, auto-convert IMG naming to SDS, and export images or PDFs.")

left, right = st.columns(2)
with left:
    st.subheader("Paste URLs")
    url_text = st.text_area(
        "One image URL per line",
        height=240,
        placeholder="https://example.com/image1.jpg\nhttps://example.com/image2.jpg",
    )
with right:
    st.subheader("Or upload TXT / CSV")
    uploaded_file = st.file_uploader("Upload a TXT or CSV file", type=["txt", "csv"])

st.subheader("Options")
col1, col2, col3 = st.columns(3)
with col1:
    naming_mode = st.selectbox(
        "Filename source",
        ["Original name from server", "CDN or URL name", "Custom prefix + serial"],
        index=0,
    )
    auto_sds = st.checkbox("Auto convert IMG naming to SDS for PXM")
    custom_prefix = st.text_input("Custom prefix", value="image")
with col2:
    output_mode = st.selectbox(
        "Output mode",
        ["Images only", "One PDF per image", "One merged PDF"],
        index=1,
    )
    page_mode = st.selectbox("Page setup", ["Original image size", "A4", "Letter"], index=1)
    fit_mode = st.selectbox("Image fit", ["Fit inside page", "Fill page"], index=0)
with col3:
    margin = st.slider("Page margin", min_value=0, max_value=40, value=20, step=2)
    merged_pdf_name = st.text_input("Merged PDF name", value="merged_images")
    st.info("A4 uses proper portrait or landscape page size based on image orientation.")

urls = []
if url_text.strip():
    urls.extend(parse_urls_from_text(url_text))
if uploaded_file is not None:
    urls.extend(parse_urls_from_uploaded_file(uploaded_file))
urls = dedupe_keep_order(urls)

st.write(f"Total valid URLs found: **{len(urls)}**")

if st.button("Start Processing", type="primary", use_container_width=True):
    if not urls:
        st.error("Please paste URLs or upload a file first.")
    else:
        with st.spinner("Downloading and processing files..."):
            results = run_bulk_download(urls, naming_mode, custom_prefix, auto_sds)

        success_count = sum(1 for r in results if r["status"] == "success")
        failed_count = sum(1 for r in results if r["status"] == "failed")
        st.success(f"Done. Success: {success_count} | Failed: {failed_count}")

        preview_rows = []
        for row in results:
            preview_rows.append(
                {
                    "status": row["status"],
                    "image_name": row["image_name"],
                    "pdf_name": row["pdf_name"],
                    "name_source": row["name_source"],
                    "http_status": row["http_status"],
                    "url": row["url"],
                    "error": row["error"],
                }
            )
        st.dataframe(preview_rows, use_container_width=True)

        zip_bytes, zip_name = build_zip(results, output_mode, page_mode, fit_mode, margin, merged_pdf_name)
        st.download_button(
            label="Download ZIP",
            data=zip_bytes,
            file_name=zip_name,
            mime="application/zip",
            use_container_width=True,
        )

st.markdown(
    """
    <hr style="margin-top:30px; margin-bottom:10px;">
    <div style="text-align:center; color:gray; font-size:14px;">
        © Designed and Developed by Pratik Adsare
    </div>
    """,
    unsafe_allow_html=True,
)
