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
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter

st.set_page_config(page_title="Bulk Image PDF Tool", page_icon="📄", layout="wide")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
REQUEST_TIMEOUT = 60
MAX_WORKERS = 8
A4_PORTRAIT = (595.28, 841.89)
A4_LANDSCAPE = (841.89, 595.28)
LETTER_PORTRAIT = (612, 792)
LETTER_LANDSCAPE = (792, 612)


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
    base = re.sub(r"_(ISP_?\d+|ISPXX|ISPXX_?\d*|IMG\d*|IMAGE\d*|IMAGE|IMG)$", "", base, flags=re.I)
    base = re.sub(r"_+$", "", base)
    if not re.search(r"_SDS$", base, flags=re.I):
        base = base + "_SDS"
    return base


def dedupe_keep_order(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def parse_urls_from_text(text: str) -> list:
    urls = []
    for line in text.splitlines():
        value = line.strip()
        if value.startswith("http://") or value.startswith("https://"):
            urls.append(value)
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
        "source_type": "url",
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
            "source_type": "url",
            "url": url,
            "final_url": "",
            "status": "failed",
            "original_name": get_name_from_url(url),
            "name_source": "url",
            "content_type": "",
            "http_status": "",
            "error": str(e),
            "bytes": b"",
        }


def load_images(urls: list) -> list:
    results = []
    progress = st.progress(0, text="Loading image URLs...")
    status_box = st.empty()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_wrapper, url): url for url in urls}
        total = len(futures)
        completed = 0
        for future in as_completed(futures):
            results.append(future.result())
            completed += 1
            progress.progress(completed / total, text=f"Loading image URLs... {completed}/{total}")
            status_box.info(f"Loaded {completed} of {total}")
    status_box.success(f"Loaded {completed} of {total}")
    return results


def load_uploaded_images(files) -> list:
    items = []
    total = len(files)
    progress = st.progress(0, text="Reading uploaded images...")
    status_box = st.empty()

    for idx, file in enumerate(files, start=1):
        try:
            data = file.read()
            content_type = getattr(file, "type", "") or "image/jpeg"
            original_name = sanitize_filename(file.name)
            original_name = ensure_extension(original_name, content_type, original_name)
            items.append(
                {
                    "source_type": "upload",
                    "url": "",
                    "final_url": "",
                    "status": "success",
                    "original_name": original_name,
                    "name_source": "uploaded-file",
                    "content_type": content_type,
                    "http_status": "",
                    "error": "",
                    "bytes": data,
                }
            )
        except Exception as e:
            items.append(
                {
                    "source_type": "upload",
                    "url": "",
                    "final_url": "",
                    "status": "failed",
                    "original_name": getattr(file, "name", "uploaded_file"),
                    "name_source": "uploaded-file",
                    "content_type": "",
                    "http_status": "",
                    "error": str(e),
                    "bytes": b"",
                }
            )
        progress.progress(idx / total, text=f"Reading uploaded images... {idx}/{total}")
        status_box.info(f"Read {idx} of {total}")

    status_box.success(f"Read {total} of {total}")
    return items


def get_page_dimensions(page_mode: str, image_width: int, image_height: int):
    if page_mode == "Original image size":
        return image_width, image_height

    is_landscape = image_width > image_height
    if page_mode == "A4":
        return A4_LANDSCAPE if is_landscape else A4_PORTRAIT
    return LETTER_LANDSCAPE if is_landscape else LETTER_PORTRAIT


def image_bytes_to_pdf_bytes(image_bytes: bytes, page_mode: str, fit_mode: str, margin: int) -> bytes:
    image = Image.open(io.BytesIO(image_bytes))
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")

    img_width, img_height = image.size
    page_width, page_height = get_page_dimensions(page_mode, img_width, img_height)

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
    results_table = []
    zip_buffer = io.BytesIO()
    merged_pdf_bytes = None

    progress = st.progress(0, text="Preparing output...")
    status_box = st.empty()

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        report_buffer = io.StringIO()
        writer = csv.DictWriter(
            report_buffer,
            fieldnames=["source_type", "original_name", "new_name", "status", "name_source", "url", "error"],
        )
        writer.writeheader()

        if output_mode in ["Images only", "One PDF per image", "Images + One PDF per image"]:
            used_names = set()
            total = len(items)
            for idx, item in enumerate(items, start=1):
                row = {
                    "source_type": item.get("source_type", ""),
                    "original_name": item.get("original_name", ""),
                    "new_name": item.get("new_name", ""),
                    "status": item.get("status", ""),
                    "name_source": item.get("name_source", ""),
                    "url": item.get("url", ""),
                    "error": item.get("error", ""),
                }
                writer.writerow(row)
                results_table.append(row)

                if item["status"] == "success":
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

                progress.progress(idx / total, text=f"Preparing output... {idx}/{total}")
                status_box.info(f"Processed {idx} of {total}")
        else:
            writer_pdf = PdfWriter()
            total = len(items)
            for idx, item in enumerate(items, start=1):
                row = {
                    "source_type": item.get("source_type", ""),
                    "original_name": item.get("original_name", ""),
                    "new_name": item.get("new_name", ""),
                    "status": item.get("status", ""),
                    "name_source": item.get("name_source", ""),
                    "url": item.get("url", ""),
                    "error": item.get("error", ""),
                }
                writer.writerow(row)
                results_table.append(row)

                if item["status"] == "success":
                    single_pdf_bytes = image_bytes_to_pdf_bytes(item["bytes"], page_mode, fit_mode, margin)
                    reader = PdfReader(io.BytesIO(single_pdf_bytes))
                    for page in reader.pages:
                        writer_pdf.add_page(page)

                progress.progress(idx / total, text=f"Building merged PDF... {idx}/{total}")
                status_box.info(f"Processed {idx} of {total}")

            status_box.info("Finalizing merged PDF...")
            merged_buffer = io.BytesIO()
            writer_pdf.write(merged_buffer)
            merged_buffer.seek(0)
            merged_pdf_bytes = merged_buffer.getvalue()
            zf.writestr("merged_output.pdf", merged_pdf_bytes)

        status_box.info("Creating ZIP package...")
        zf.writestr("download_report.csv", report_buffer.getvalue().encode("utf-8-sig"))

    zip_buffer.seek(0)
    progress.progress(1.0, text="Output package ready")
    status_box.success("ZIP package is ready")
    return zip_buffer.getvalue(), merged_pdf_bytes, results_table


def reset_loaded_state():
    st.session_state["loaded_items"] = []
    st.session_state["results_table"] = []
    st.session_state["zip_bytes"] = None
    st.session_state["merged_pdf_bytes"] = None


if "loaded_items" not in st.session_state:
    st.session_state["loaded_items"] = []
if "results_table" not in st.session_state:
    st.session_state["results_table"] = []
if "zip_bytes" not in st.session_state:
    st.session_state["zip_bytes"] = None
if "merged_pdf_bytes" not in st.session_state:
    st.session_state["merged_pdf_bytes"] = None


st.title("Bulk Image Downloader and PDF Converter")
st.caption("Upload images or paste image URLs, rename row by row, auto-convert IMG naming to SDS, and download output while the tab stays open.")

url_text = st.text_area(
    "Paste image URLs",
    height=180,
    placeholder="One image URL per line",
)

uploaded_images = st.file_uploader(
    "Upload images directly",
    type=["jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff"],
    accept_multiple_files=True,
)

opt1, opt2, opt3 = st.columns(3)
with opt1:
    output_mode = st.selectbox(
        "Output mode",
        ["One PDF per image", "One merged PDF", "Images only", "Images + One PDF per image"],
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
        urls = parse_urls_from_text(url_text) if url_text.strip() else []
        has_uploaded_images = uploaded_images is not None and len(uploaded_images) > 0

        if not urls and not has_uploaded_images:
            st.error("Please paste image URLs or upload images first.")
        else:
            reset_loaded_state()
            items = []
            with st.spinner("Loading images and reading names..."):
                if urls:
                    items.extend(load_images(urls))
                if has_uploaded_images:
                    items.extend(load_uploaded_images(uploaded_images))

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

    action_col1, action_col2 = st.columns(2)
    with action_col1:
        if st.button("Apply SDS naming"):
            for item in loaded_items:
                if item["status"] == "success":
                    item["new_name"] = convert_image_name_to_sds(item["original_name"])
            st.session_state["loaded_items"] = loaded_items
            st.rerun()
    with action_col2:
        if st.button("Use current names"):
            for item in loaded_items:
                if item["status"] == "success":
                    item["new_name"] = get_base_name(item["original_name"])
            st.session_state["loaded_items"] = loaded_items
            st.rerun()

    rows_to_show = st.selectbox("Rows visible", [10, 20, 30, 50], index=1)

    with st.container(border=True):
        for idx, item in enumerate(loaded_items[:rows_to_show]):
            c1, c2, c3, c4 = st.columns([1.4, 4.2, 4.2, 1.6])
            with c1:
                st.write(item["source_type"].upper())
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

        if len(loaded_items) > rows_to_show:
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
            with st.spinner("Processing output files and creating ZIP..."):
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
    if output_mode == "One merged PDF" and st.session_state["merged_pdf_bytes"] is not None:
        st.download_button(
            "Download Merged PDF",
            data=st.session_state["merged_pdf_bytes"],
            file_name="merged_output.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

st.info("Keep this tab open while processing. In this Streamlit-only version, long jobs are not reliable if the session disconnects or the tab sleeps.")

st.markdown(
    """
    <hr style='margin-top:30px; margin-bottom:10px;'>
    <div style='text-align:center; color:gray; font-size:14px;'>
        © Designed and Developed by Pratik Adsare
    </div>
    """,
    unsafe_allow_html=True,
)
