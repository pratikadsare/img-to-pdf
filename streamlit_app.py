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

st.set_page_config(page_title="Bulk Image PDF Tool", page_icon="📄", layout="wide")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
REQUEST_TIMEOUT = 60
MAX_WORKERS = 8
A4_PORTRAIT = (595.28, 841.89)
A4_LANDSCAPE = (841.89, 595.28)
LETTER_PORTRAIT = (612, 792)
LETTER_LANDSCAPE = (792, 612)


# -----------------------------
# Helpers
# -----------------------------
def sanitize_filename(name: str) -> str:
    name = str(name).strip().replace("\x00", "")
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "downloaded_file"


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


def get_base_name(filename: str) -> str:
    return re.sub(r"\.[^.]+$", "", str(filename))


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
        "application/pdf": ".pdf",
    }
    return mapping.get(content_type, "")


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


def make_unique_name(filename: str, used_names: set) -> str:
    base = Path(filename).stem
    ext = Path(filename).suffix
    candidate = filename
    counter = 1
    while candidate.lower() in used_names:
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    used_names.add(candidate.lower())
    return candidate


def convert_image_name_to_sds(name: str) -> str:
    base = sanitize_filename(get_base_name(name))
    base = re.sub(r"_(ISP_?\d+|ISPXX|IMG\d*|IMAGE\d*|IMAGE|IMG)$", "", base, flags=re.I)
    base = re.sub(r"_+$", "", base)
    if not re.search(r"_SDS$", base, flags=re.I):
        base = base + "_SDS"
    return base


def parse_urls_from_text(text: str) -> list:
    urls = []
    for line in text.splitlines():
        value = line.strip()
        if value.startswith("http://") or value.startswith("https://"):
            urls.append(value)
    return dedupe_keep_order(urls)


def parse_urls_from_uploaded_file(uploaded_file) -> list:
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


def dedupe_keep_order(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


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


def fetch_image(url: str) -> dict:
    session = build_session()
    response = session.get(url, stream=True, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    content_disposition = response.headers.get("Content-Disposition", "")
    header_name = get_name_from_content_disposition(content_disposition)
    url_name = get_name_from_url(response.url) or get_name_from_url(url)
    original_name = header_name or url_name or "downloaded_file.jpg"
    original_name = ensure_extension(original_name, content_type, response.url)
    original_name = sanitize_filename(original_name)

    content = io.BytesIO()
    for chunk in response.iter_content(chunk_size=1024 * 64):
        if chunk:
            content.write(chunk)
    data = content.getvalue()

    return {
        "url": url,
        "final_url": response.url,
        "status": "success",
        "original_name": original_name,
        "name_source": "content-disposition" if header_name else "url",
        "content_type": content_type,
        "http_status": response.status_code,
        "error": "",
        "bytes": data,
    }


def fetch_wrapper(url: str) -> dict:
    try:
        return fetch_image(url)
    except Exception as e:
        return {
            "url": url,
            "final_url": "",
            "status": "failed",
            "original_name": "",
            "name_source": "",
            "content_type": "",
            "http_status": "",
            "error": str(e),
            "bytes": b"",
        }


def load_images(urls: list) -> list:
    results = []
    progress = st.progress(0)
    status = st.empty()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_wrapper, url): url for url in urls}
        total = len(futures)
        completed = 0
        for future in as_completed(futures):
            results.append(future.result())
            completed += 1
            progress.progress(completed / total)
            status.info(f"Loaded {completed} of {total}")
    status.success(f"Loaded {completed} of {total}")
    return results


def get_page_dimensions(page_mode: str, image_width: int, image_height: int):
    if page_mode == "Original image size":
        return "px", (image_width, image_height)

    is_landscape = image_width > image_height
    if page_mode == "A4":
        return "pt", A4_LANDSCAPE if is_landscape else A4_PORTRAIT
    return "pt", LETTER_LANDSCAPE if is_landscape else LETTER_PORTRAIT


def image_bytes_to_pdf_bytes(image_bytes: bytes, page_mode: str, fit_mode: str, margin: int) -> bytes:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = Image.open(io.BytesIO(image_bytes))
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")

    img_width, img_height = image.size
    unit, page_size = get_page_dimensions(page_mode, img_width, img_height)
    page_width, page_height = page_size

    if page_mode == "Original image size":
        margin = 0

    content_width = max(page_width - (margin * 2), 1)
    content_height = max(page_height - (margin * 2), 1)
    image_ratio = img_width / img_height
    box_ratio = content_width / content_height

    if fit_mode == "Fill page":
        if image_ratio > box_ratio:
            draw_height = content_height
            draw_width = draw_height * image_ratio
        else:
            draw_width = content_width
            draw_height = draw_width / image_ratio
    else:
        if image_ratio > box_ratio:
            draw_width = content_width
            draw_height = draw_width / image_ratio
        else:
            draw_height = content_height
            draw_width = draw_height * image_ratio

    x = (page_width - draw_width) / 2
    y = (page_height - draw_height) / 2

    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=(page_width, page_height))
    pdf.drawImage(ImageReader(image), x, y, width=draw_width, height=draw_height, preserveAspectRatio=False, mask='auto')
    pdf.showPage()
    pdf.save()
    out.seek(0)
    return out.getvalue()


def build_outputs(items: list, output_mode: str, page_mode: str, fit_mode: str, margin: int):
    used_names = set()
    results_table = []
    zip_buffer = io.BytesIO()
    merged_pdf_buffer = io.BytesIO()
    merged_files = []

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        report_buffer = io.StringIO()
        writer = csv.DictWriter(
            report_buffer,
            fieldnames=["original_name", "new_name", "status", "name_source", "url", "error"],
        )
        writer.writeheader()

        for item in items:
            row = {
                "original_name": item.get("original_name", ""),
                "new_name": item.get("new_name", ""),
                "status": item.get("status", ""),
                "name_source": item.get("name_source", ""),
                "url": item.get("url", ""),
                "error": item.get("error", ""),
            }
            writer.writerow(row)
            results_table.append(row)

            if item["status"] != "success":
                continue

            image_name = make_unique_name(item["new_name"] + item["image_ext"], used_names)
            pdf_name = make_unique_name(item["new_name"] + ".pdf", used_names)

            if output_mode == "Images only":
                zf.writestr(image_name, item["bytes"])
            elif output_mode == "One PDF per image":
                pdf_bytes = image_bytes_to_pdf_bytes(item["bytes"], page_mode, fit_mode, margin)
                zf.writestr(pdf_name, pdf_bytes)
            elif output_mode == "Images + One PDF per image":
                zf.writestr(image_name, item["bytes"])
                pdf_bytes = image_bytes_to_pdf_bytes(item["bytes"], page_mode, fit_mode, margin)
                zf.writestr(pdf_name, pdf_bytes)
            else:
                merged_files.append((item["new_name"] + ".pdf", item["bytes"]))

        zf.writestr("download_report.csv", report_buffer.getvalue().encode("utf-8-sig"))

    merged_pdf_bytes = None
    if output_mode == "One merged PDF" and merged_files:
        from pypdf import PdfWriter, PdfReader

        writer = PdfWriter()
        for _, image_bytes in merged_files:
            single_pdf_bytes = image_bytes_to_pdf_bytes(image_bytes, page_mode, fit_mode, margin)
            reader = PdfReader(io.BytesIO(single_pdf_bytes))
            for page in reader.pages:
                writer.add_page(page)
        writer.write(merged_pdf_buffer)
        merged_pdf_buffer.seek(0)
        merged_pdf_bytes = merged_pdf_buffer.getvalue()

    zip_buffer.seek(0)
    return zip_buffer.getvalue(), merged_pdf_bytes, results_table


def reset_loaded_state():
    st.session_state["loaded_items"] = []
    st.session_state["results_table"] = []
    st.session_state["zip_bytes"] = None
    st.session_state["merged_pdf_bytes"] = None


# -----------------------------
# Session state init
# -----------------------------
if "loaded_items" not in st.session_state:
    st.session_state["loaded_items"] = []
if "results_table" not in st.session_state:
    st.session_state["results_table"] = []
if "zip_bytes" not in st.session_state:
    st.session_state["zip_bytes"] = None
if "merged_pdf_bytes" not in st.session_state:
    st.session_state["merged_pdf_bytes"] = None


# -----------------------------
# UI
# -----------------------------
st.title("Bulk Image Downloader and PDF Converter")
st.caption("Paste image URLs, load original names, rename in bulk, auto-convert IMG naming to SDS, and download files.")

left, right = st.columns(2)
with left:
    url_text = st.text_area(
        "Paste image URLs",
        height=220,
        placeholder="One image URL per line",
    )
with right:
    uploaded_file = st.file_uploader("Upload TXT or CSV with URLs", type=["txt", "csv"])

opt1, opt2, opt3 = st.columns(3)
with opt1:
    output_mode = st.selectbox(
        "Output mode",
        ["Images only", "One PDF per image", "One merged PDF", "Images + One PDF per image"],
    )
with opt2:
    page_mode = st.selectbox("Page setup", ["A4", "Letter", "Original image size"])
with opt3:
    fit_mode = st.selectbox("Image fit", ["Fit inside page", "Fill page"])

opt4, opt5 = st.columns(2)
with opt4:
    margin = st.slider("Page margin", min_value=0, max_value=40, value=20)
with opt5:
    auto_sds = st.checkbox("Auto convert IMG naming to SDS naming for PXM")

load_col1, load_col2 = st.columns(2)
with load_col1:
    if st.button("Load files", type="primary", use_container_width=True):
        urls = []
        if url_text.strip():
            urls.extend(parse_urls_from_text(url_text))
        if uploaded_file is not None:
            urls.extend(parse_urls_from_uploaded_file(uploaded_file))
        urls = dedupe_keep_order(urls)

        if not urls:
            st.error("Please paste URLs or upload a file first.")
        else:
            reset_loaded_state()
            with st.spinner("Loading images and reading names..."):
                items = load_images(urls)
            for item in items:
                if item["status"] == "success":
                    item["image_ext"] = Path(item["original_name"]).suffix or get_extension_from_content_type(item["content_type"]) or ".jpg"
                    item["new_name"] = convert_image_name_to_sds(item["original_name"]) if auto_sds else get_base_name(item["original_name"])
                else:
                    item["image_ext"] = ".jpg"
                    item["new_name"] = ""
            st.session_state["loaded_items"] = items
            st.success(f"Loaded {len(items)} item(s).")
with load_col2:
    if st.button("Clear all", use_container_width=True):
        reset_loaded_state()
        st.rerun()

loaded_items = st.session_state["loaded_items"]

if loaded_items:
    st.subheader("Rename mapping")

    bulk_col1, bulk_col2, bulk_col3 = st.columns(3)
    with bulk_col1:
        if st.button("Apply SDS naming"):
            for item in loaded_items:
                if item["status"] == "success":
                    item["new_name"] = convert_image_name_to_sds(item["original_name"])
            st.session_state["loaded_items"] = loaded_items
            st.rerun()
    with bulk_col2:
        if st.button("Use current names"):
            for item in loaded_items:
                if item["status"] == "success":
                    item["new_name"] = get_base_name(item["original_name"])
            st.session_state["loaded_items"] = loaded_items
            st.rerun()
    with bulk_col3:
        bulk_names = st.text_area("Bulk paste new names", height=100, placeholder="Paste one new name per line")
        if st.button("Apply pasted names"):
            names = [sanitize_filename(x) for x in bulk_names.splitlines() if x.strip()]
            success_items = [x for x in loaded_items if x["status"] == "success"]
            for idx, name in enumerate(names):
                if idx < len(success_items):
                    success_items[idx]["new_name"] = get_base_name(name)
            st.session_state["loaded_items"] = loaded_items
            st.rerun()

    rows_to_show = st.selectbox("Rows visible", [10, 20, 30, 50], index=1)
    visible_items = loaded_items[:]

    with st.container(border=True):
        for idx, item in enumerate(visible_items[:rows_to_show]):
            c1, c2, c3, c4 = st.columns([1, 4, 4, 2])
            with c1:
                st.write("URL")
            with c2:
                st.text(item["original_name"] if item["original_name"] else item["url"])
            with c3:
                if item["status"] == "success":
                    new_val = st.text_input(
                        f"new_name_{idx}",
                        value=item["new_name"],
                        label_visibility="collapsed",
                    )
                    item["new_name"] = sanitize_filename(get_base_name(new_val))
                else:
                    st.text("Failed to load")
            with c4:
                if item["status"] == "success":
                    st.success("OK")
                else:
                    st.error("Failed")
        if len(visible_items) > rows_to_show:
            st.info(f"Showing first {rows_to_show} rows. Increase 'Rows visible' to see more.")

    st.session_state["loaded_items"] = loaded_items

    if st.button("Process files", type="primary", use_container_width=True):
        valid_names = []
        has_error = False
        for item in loaded_items:
            if item["status"] != "success":
                continue
            if not item["new_name"]:
                has_error = True
                break
            valid_names.append(item["new_name"].lower())
        if has_error:
            st.error("All successful rows must have a new file name.")
        elif len(valid_names) != len(set(valid_names)):
            st.error("Duplicate new file names found. Please make them unique.")
        else:
            with st.spinner("Processing output files..."):
                zip_bytes, merged_pdf_bytes, results_table = build_outputs(
                    loaded_items,
                    output_mode,
                    page_mode,
                    fit_mode,
                    margin,
                )
            st.session_state["zip_bytes"] = zip_bytes
            st.session_state["merged_pdf_bytes"] = merged_pdf_bytes
            st.session_state["results_table"] = results_table
            st.success("Processing complete.")

results_table = st.session_state["results_table"]
if results_table:
    st.subheader("Processing report")
    st.dataframe(results_table, use_container_width=True)

if st.session_state["zip_bytes"] is not None or st.session_state["merged_pdf_bytes"] is not None:
    st.subheader("Download output")
    if st.session_state["zip_bytes"] is not None:
        st.download_button(
            "Download ZIP",
            data=st.session_state["zip_bytes"],
            file_name="bulk_output.zip",
            mime="application/zip",
            use_container_width=True,
        )
    if st.session_state["merged_pdf_bytes"] is not None:
        st.download_button(
            "Download Merged PDF",
            data=st.session_state["merged_pdf_bytes"],
            file_name="merged_output.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

st.markdown(
    """
    <hr style='margin-top:30px; margin-bottom:10px;'>
    <div style='text-align:center; color:gray; font-size:14px;'>
        © Designed and Developed by Pratik Adsare
    </div>
    """,
    unsafe_allow_html=True,
)
