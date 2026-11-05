"""rasa engine: permanent PDF redaction, driven over JSON lines on stdin/stdout.

Each request is one line: {"id": n, "cmd": "...", "args": {...}}.
Each reply is one line:   {"id": n, "ok": true, "result": ...} or {"id": n, "ok": false, "error": "..."}.

Redaction deletes the underlying characters, image pixels and covered vector
graphics (MuPDF's redaction), then strips everything outside the page content
that could carry the same words: metadata, XMP, bookmarks, links, annotations,
form fields, attachments, JavaScript, thumbnails and invisible text. The file is
written fresh (no incremental update) with unused objects garbage-collected, then
re-opened and checked.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys

import pymupdf

RENDER_ZOOM = 2.0  # 144 dpi previews


def _norm(s: str) -> str:
    """Case-fold and collapse whitespace so 'John\n Smith' matches 'john smith'."""
    return re.sub(r"\s+", " ", s).strip().casefold()


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def _rect(r) -> list[float]:
    return [round(r.x0, 3), round(r.y0, 3), round(r.x1, 3), round(r.y1, 3)]


def load(path: str) -> pymupdf.Document:
    """Open a PDF and normalise it so what you see is what gets searched and redacted."""
    doc = pymupdf.open(path)
    if not doc.is_pdf:
        raise ValueError("That file is not a PDF.")
    if doc.needs_pass:
        raise ValueError("This PDF is password-protected. Remove the password first.")
    # Turn annotations and form fields into ordinary page content, so search
    # finds their text and redaction removes it like anything else.
    doc.bake()
    for page in doc:
        if page.rotation:
            page.remove_rotation()  # keeps preview, search and redaction in one coordinate space
    return doc


def unreadable(page: pymupdf.Page) -> bool:
    """True when a page shows content but has no extractable text.

    "Microsoft Print to PDF" and scanners produce these: every letter is a
    drawn shape or pixels, so search cannot see it and neither can verify().
    """
    if page.get_text("text").strip():
        return False
    return bool(page.get_images() or page.get_drawings())


# ---------------------------------------------------------------- OCR
# Windows' built-in text recognition (Windows.Media.Ocr): no model to ship,
# nothing leaves the machine. Used only on pages with no extractable text.

OCR_ZOOM = 3.0  # ~216 dpi
OCR_PAD = 1.0  # points added around each recognised word; OCR boxes run tight

# OCR confuses these. Folding both the term and the recognised text the same
# way means "gmaiI.com" still matches "gmail.com". Over-matching is safe here.
_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "!": "l"})


class OcrUnavailable(RuntimeError):
    pass


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold().translate(_CONFUSABLE)


def ocr_available() -> bool:
    try:
        _ocr_engine()
        return True
    except OcrUnavailable:
        return False


_engine_cache = []


def _ocr_engine():
    if _engine_cache:
        return _engine_cache[0]
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError as e:
        raise OcrUnavailable("Text recognition needs Windows.") from e
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise OcrUnavailable("Windows has no text-recognition language installed.")
    _engine_cache.append(engine)
    return engine


def ocr_lines(page: pymupdf.Page) -> list[list[tuple[str, pymupdf.Rect]]]:
    """Recognise a page. Returns lines of (word, rect in PDF points)."""
    import asyncio

    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = _ocr_engine()
    zoom = min(OCR_ZOOM, (OcrEngine.max_image_dimension - 1) / max(page.rect.width, page.rect.height))
    # Opaque white background first, then add an alpha channel: a transparent
    # background reads as black and the black text disappears.
    pix = pymupdf.Pixmap(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False), 1)
    bgra = bytearray(pix.samples)
    bgra[0::4], bgra[2::4] = bgra[2::4], bgra[0::4]
    writer = DataWriter()
    writer.write_bytes(bgra)
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, pix.width, pix.height, BitmapAlphaMode.PREMULTIPLIED
    )

    async def run():
        return await engine.recognize_async(bmp)

    result = asyncio.run(run())
    lines = []
    for line in result.lines:
        words = []
        for w in line.words:
            b = w.bounding_rect
            words.append((w.text, pymupdf.Rect(b.x, b.y, b.x + b.width, b.y + b.height) / zoom))
        lines.append(words)
    return lines


def ocr_find(lines: list[list[tuple[str, pymupdf.Rect]]], term: str) -> list[pymupdf.Rect]:
    """Find a term in recognised lines; each hit covers the whole words it touches."""
    t = _fold(term)
    if not t:
        return []
    hits = []
    for words in lines:
        text, spans = "", []
        for word, rect in words:
            if text:
                text += " "
            start = len(text)
            text += _fold(word)
            spans.append((start, len(text), rect))
        i = text.find(t)
        while i != -1:
            end = i + len(t)
            rects = [r for a, b, r in spans if a < end and b > i]
            box = pymupdf.Rect(rects[0])
            for r in rects[1:]:
                box |= r
            hits.append(box + (-OCR_PAD, -OCR_PAD, OCR_PAD, OCR_PAD))
            i = text.find(t, i + 1)
    return hits


# ---------------------------------------------------------------- search


def search(doc: pymupdf.Document, terms: list[str], ocr: dict | None = None) -> list[dict]:
    """Find every instance of each term, widened to whole words.

    Widening means "Barclay" removes all of "adbarclay92@gmail.com" rather than
    leaving "ad…92@gmail.com" readable on either side of the box.
    `ocr` maps page number -> ocr_lines() for pages without a text layer.
    """
    ocr = ocr or {}
    hits = []
    for term in terms:
        t = re.sub(r"\s+", " ", term).strip()
        if not t:
            continue
        for page in doc:
            if page.number in ocr:
                for r in ocr_find(ocr[page.number], t):
                    hits.append({"page": page.number, "rect": _rect(r), "term": t, "ocr": True})
                continue
            words = None
            for r in page.search_for(t):
                if words is None:
                    words = [pymupdf.Rect(w[:4]) for w in page.get_text("words")]
                grown = pymupdf.Rect(r)
                for wr in words:
                    inter = wr & r
                    if not inter.is_empty and inter.get_area() > 0.2 * min(wr.get_area(), r.get_area()):
                        grown |= wr
                hits.append({"page": page.number, "rect": _rect(grown), "term": t})
    return hits


def _snap(rect: pymupdf.Rect, drawings: list[dict]) -> pymupdf.Rect:
    """Grow a box to fully cover any letter-sized shape whose centre it covers.

    MuPDF only deletes vector shapes that lie entirely inside a box. In files
    where letters are shapes, a box drawn slightly short would leave a
    half-covered letter in the file, recoverable by removing the black fill.
    """
    grown = pymupdf.Rect(rect)
    for dr in drawings:
        b = dr["rect"]
        if b.width <= 40 and b.height <= 40:
            centre = pymupdf.Point((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)
            if centre in rect:
                grown |= b
    return grown


def redact(doc: pymupdf.Document, redactions: list[dict]) -> None:
    """Apply redactions in place and scrub everything outside page content."""
    by_page: dict[int, list] = {}
    for r in redactions:
        by_page.setdefault(int(r["page"]), []).append(pymupdf.Rect(r["rect"]))
    for pno, rects in by_page.items():
        page = doc[pno]
        drawings = page.get_drawings()
        for rect in rects:
            page.add_redact_annot(_snap(rect, drawings), fill=(0, 0, 0))
    for page in doc:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,  # blank the covered pixels, not just cover them
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    doc.scrub(
        attached_files=True,
        clean_pages=True,
        embedded_files=True,
        hidden_text=True,  # invisible text layers can hold words you never see
        javascript=True,
        metadata=True,
        redactions=False,  # already applied above with stricter options
        remove_links=True,  # mailto: and URL targets often contain names
        reset_fields=True,
        reset_responses=True,
        thumbnails=True,
        xml_metadata=True,
    )
    doc.set_toc([])
    doc.set_metadata({})
    for page in doc:  # anything bake() left behind
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
    for name in doc.embfile_names():
        doc.embfile_del(name)


def save(doc: pymupdf.Document, out_path: str) -> None:
    doc.save(
        out_path,
        garbage=4,  # drop unreferenced objects, including the originals of edited streams
        deflate=True,
        clean=True,
        incremental=False,
        encryption=pymupdf.PDF_ENCRYPT_NONE,
    )


def verify(out_path: str, terms: list[str], regions: list[dict]) -> dict:
    """Re-open the saved file and look for anything that should be gone."""
    findings: list[str] = []
    doc = pymupdf.open(out_path)
    terms = [t for t in (re.sub(r"\s+", " ", t).strip() for t in terms) if t]

    # 1. Extractable text, page by page (normal, whitespace-insensitive, and the search engine itself).
    for page in doc:
        text = page.get_text("text")
        for t in terms:
            if _norm(t) in _norm(text) or _squash(t) in _squash(text) or page.search_for(t):
                findings.append(f'"{t}" is still readable on page {page.number + 1}.')

    # 2. Nothing left inside any redacted area.
    for r in regions:
        page = doc[int(r["page"])]
        rect = pymupdf.Rect(r["rect"])
        left = []  # characters whose centre is inside the box (MuPDF's own removal rule)
        for block in page.get_text("rawdict", clip=rect)["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for ch in span["chars"]:
                        b = pymupdf.Rect(ch["bbox"])
                        centre = pymupdf.Point((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)
                        if centre in rect and ch["c"].strip():
                            left.append(ch["c"])
        if left:
            findings.append(f'Text "{"".join(left)}" remains under a redaction on page {page.number + 1}.')

    # 3. Raw bytes of every object and decompressed stream.
    needles = set()
    for t in terms:
        for variant in {t, _squash(t)}:
            for enc in ("latin-1", "utf-8", "utf-16-be"):
                try:
                    needles.add(variant.casefold().encode(enc))
                except UnicodeEncodeError:
                    pass
    needles.discard(b"")
    for xref in range(1, doc.xref_length()):
        blobs = []
        try:
            blobs.append(doc.xref_object(xref, compressed=False).encode("latin-1", "replace"))
        except Exception:
            pass
        try:
            if doc.xref_is_stream(xref):
                blobs.append(doc.xref_stream(xref) or b"")
        except Exception:
            pass
        for blob in blobs:
            low = blob.lower()
            for n in needles:
                if n in low:
                    findings.append(f"A searched term appears in the raw bytes of PDF object {xref}.")
                    break

    # 4. Places outside the page that must be empty.
    if any(v for k, v in doc.metadata.items() if v and k not in ("format", "encryption")):
        findings.append("Document metadata is not empty.")
    if doc.get_xml_metadata():
        findings.append("XMP metadata is not empty.")
    if doc.get_toc():
        findings.append("Bookmarks are still present.")
    if doc.embfile_count():
        findings.append("Attached files are still present.")
    for page in doc:
        if page.first_annot is not None or page.first_widget is not None:
            findings.append(f"Annotations or form fields remain on page {page.number + 1}.")

    # 5. Pages without a text layer: read them with OCR, or report that they could not be checked.
    blind, ocr_checked = [], []
    can_ocr = ocr_available()
    for page in doc:
        if not unreadable(page):
            continue
        if not can_ocr:
            blind.append(page.number + 1)
            continue
        ocr_checked.append(page.number + 1)
        lines = ocr_lines(page)
        for t in terms:
            if ocr_find(lines, t):
                findings.append(f'"{t}" can still be read (by text recognition) on page {page.number + 1}.')

    doc.close()
    return {
        "ok": not findings,
        "findings": sorted(set(findings), key=findings.index),
        "unreadable_pages": blind,  # not checked at all
        "ocr_pages": ocr_checked,  # checked by OCR, which can misread
    }


# ---------------------------------------------------------------- protocol


REDACTED_SUFFIX = "-redacted"


class Session:
    """Open documents, keyed by path. Commands act on `path`, or the last one opened."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}  # path -> {"doc": Document, "ocr": {page: lines} | None}
        self.path: str | None = None

    def _entry(self, path: str | None = None) -> dict:
        path = path or self.path
        if path is None or path not in self.docs:
            raise ValueError("No PDF is open.")
        return self.docs[path]

    def _need(self, path: str | None = None) -> pymupdf.Document:
        return self._entry(path)["doc"]

    def _ocr_pages(self, path: str | None = None) -> dict[int, list]:
        entry = self._entry(path)
        if entry["ocr"] is None:  # recognised once, on first search
            entry["ocr"] = {}
            if ocr_available():
                for page in entry["doc"]:
                    if unreadable(page):
                        entry["ocr"][page.number] = ocr_lines(page)
        return entry["ocr"]

    def cmd_close_all(self) -> dict:
        for entry in self.docs.values():
            entry["doc"].close()
        self.docs, self.path = {}, None
        return {}

    def cmd_list_pdfs(self, folder: str) -> dict:
        """Every PDF under a folder, skipping copies rasa already made."""
        found = []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            for name in sorted(filenames):
                stem, ext = os.path.splitext(name)
                if ext.lower() == ".pdf" and not stem.endswith(REDACTED_SUFFIX):
                    found.append(os.path.join(dirpath, name))
        return {"paths": found}

    def cmd_out_path(self, path: str, folder: str, out_dir: str) -> dict:
        """Where a folder file's redacted copy goes: same subfolders, name + suffix."""
        rel = os.path.relpath(path, folder)
        stem, ext = os.path.splitext(rel)
        return {"out_path": os.path.join(out_dir, stem + REDACTED_SUFFIX + ext)}

    def cmd_open(self, path: str) -> dict:
        doc = load(path)
        if path in self.docs:
            self.docs[path]["doc"].close()
        self.docs[path] = {"doc": doc, "ocr": None}
        self.path = path
        return {
            "path": path,
            "pages": [{"width": p.rect.width, "height": p.rect.height} for p in doc],
            "unreadable_pages": [p.number + 1 for p in doc if unreadable(p)],
            "ocr_available": ocr_available(),
        }

    def cmd_render(self, page: int, zoom: float = RENDER_ZOOM, path: str | None = None) -> dict:
        pix = self._need(path)[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return {"png": base64.b64encode(pix.tobytes("png")).decode("ascii")}

    def cmd_search(self, terms: list[str], path: str | None = None) -> dict:
        return {"hits": search(self._need(path), terms, self._ocr_pages(path))}

    def cmd_apply(self, out_path: str, redactions: list[dict], terms: list[str], path: str | None = None) -> dict:
        src = path or self.path
        self._need(src)
        if os.path.abspath(out_path).casefold() == os.path.abspath(src).casefold():
            raise ValueError("Save the redacted copy under a new name; the original is kept.")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        doc = load(src)  # fresh copy, so the open session is never half-redacted
        try:
            redact(doc, redactions)
            save(doc, out_path)
        finally:
            doc.close()
        report = verify(out_path, terms, redactions)
        report["out_path"] = out_path
        report["count"] = len(redactions)
        return report


def main() -> None:
    # Keep the protocol channel clean: anything printed by libraries goes to stderr.
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)

    session = Session()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            handler = getattr(session, "cmd_" + req["cmd"], None)
            if handler is None:
                raise ValueError(f"Unknown command {req['cmd']!r}")
            reply = {"id": rid, "ok": True, "result": handler(**req.get("args", {}))}
        except Exception as e:  # report every failure to the UI instead of dying
            reply = {"id": rid, "ok": False, "error": str(e) or type(e).__name__}
        proto.write(json.dumps(reply) + "\n")
        proto.flush()


if __name__ == "__main__":
    main()
