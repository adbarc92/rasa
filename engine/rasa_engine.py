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


def search(doc: pymupdf.Document, terms: list[str]) -> list[dict]:
    hits = []
    for term in terms:
        t = re.sub(r"\s+", " ", term).strip()
        if not t:
            continue
        for page in doc:
            for r in page.search_for(t):
                hits.append({"page": page.number, "rect": _rect(r), "term": t})
    return hits


def redact(doc: pymupdf.Document, redactions: list[dict]) -> None:
    """Apply redactions in place and scrub everything outside page content."""
    by_page: dict[int, list] = {}
    for r in redactions:
        by_page.setdefault(int(r["page"]), []).append(pymupdf.Rect(r["rect"]))
    for pno, rects in by_page.items():
        page = doc[pno]
        for rect in rects:
            page.add_redact_annot(rect, fill=(0, 0, 0))
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

    doc.close()
    return {"ok": not findings, "findings": sorted(set(findings), key=findings.index)}


# ---------------------------------------------------------------- protocol


class Session:
    def __init__(self) -> None:
        self.doc: pymupdf.Document | None = None
        self.path: str | None = None

    def _need(self) -> pymupdf.Document:
        if self.doc is None:
            raise ValueError("No PDF is open.")
        return self.doc

    def cmd_open(self, path: str) -> dict:
        doc = load(path)
        if self.doc is not None:
            self.doc.close()
        self.doc, self.path = doc, path
        return {
            "path": path,
            "pages": [{"width": p.rect.width, "height": p.rect.height} for p in doc],
        }

    def cmd_render(self, page: int, zoom: float = RENDER_ZOOM) -> dict:
        pix = self._need()[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return {"png": base64.b64encode(pix.tobytes("png")).decode("ascii")}

    def cmd_search(self, terms: list[str]) -> dict:
        return {"hits": search(self._need(), terms)}

    def cmd_apply(self, out_path: str, redactions: list[dict], terms: list[str]) -> dict:
        self._need()
        if os.path.abspath(out_path).casefold() == os.path.abspath(self.path).casefold():
            raise ValueError("Save the redacted copy under a new name; the original is kept.")
        doc = load(self.path)  # fresh copy, so the open session is never half-redacted
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
