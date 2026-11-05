"""The redaction guarantee: after apply, the secret is gone from every place a PDF can hide it."""

import json
import subprocess
import sys
import zlib
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rasa_engine as eng  # noqa: E402

SECRET = "Jonathan Q. Testperson"
ACCOUNT = "4417-1234-5678-9113"


def make_tricky_pdf(path: Path) -> None:
    """A PDF that hides the secret everywhere it can."""
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 100), f"I, {SECRET}, being of sound mind, page {i + 1}.", fontsize=11)
        page.insert_text((72, 130), f"Account {ACCOUNT} passes to my heirs.", fontsize=11)
        page.insert_text((72, 160), "This sentence must survive redaction.", fontsize=11)
        # Invisible text layer (render mode 3), like OCR output.
        page.insert_text((72, 400), f"hidden {SECRET}", fontsize=11, render_mode=3)
    # An image whose pixels sit under the redaction.
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, (200, 30, 30))
    doc[0].insert_image(pymupdf.Rect(300, 250, 400, 350), pixmap=pix)
    # Annotation, form field, bookmark, metadata, link, attachment.
    doc[1].add_freetext_annot(pymupdf.Rect(72, 200, 400, 230), f"Note about {SECRET}")
    w = pymupdf.Widget()
    w.field_name = "beneficiary"
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_value = SECRET
    w.rect = pymupdf.Rect(72, 250, 300, 270)
    doc[2].add_widget(w)
    doc.set_toc([[1, f"Will of {SECRET}", 1]])
    doc.set_metadata({"author": SECRET, "title": f"Estate of {SECRET}"})
    doc.set_xml_metadata(f"<x:xmpmeta xmlns:x='adobe:ns:meta/'><r>{SECRET}</r></x:xmpmeta>")
    doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 300, 200, 320),
                        "uri": "mailto:jonathan.testperson@example.com"})
    doc.embfile_add("notes.txt", f"secret: {SECRET}".encode())
    doc.save(path)


@pytest.fixture
def tricky(tmp_path):
    p = tmp_path / "will.pdf"
    make_tricky_pdf(p)
    return p


def all_raw_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    doc = pymupdf.open(path)
    parts = [data]
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_is_stream(x):
                parts.append(doc.xref_stream(x) or b"")
        except Exception:
            pass
    return b"\n".join(parts).lower()


def run_apply(src: Path, out: Path, terms, extra=()):
    s = eng.Session()
    s.cmd_open(str(src))
    hits = s.cmd_search(terms)["hits"]
    return hits, s.cmd_apply(str(out), hits + list(extra), terms)


def test_search_finds_every_visible_instance(tricky):
    s = eng.Session()
    s.cmd_open(str(tricky))
    hits = s.cmd_search([SECRET])["hits"]
    # 3 body lines + baked annotation + baked form field (+ hidden text layers)
    assert len(hits) >= 5
    assert {h["page"] for h in hits} == {0, 1, 2}


def test_search_is_case_insensitive(tricky):
    s = eng.Session()
    s.cmd_open(str(tricky))
    assert s.cmd_search([SECRET.upper()])["hits"]


def test_apply_removes_secret_everywhere(tricky, tmp_path):
    out = tmp_path / "out.pdf"
    hits, report = run_apply(tricky, out, [SECRET, ACCOUNT])
    assert report["ok"], report["findings"]

    doc = pymupdf.open(out)
    text = "".join(p.get_text() for p in doc)
    assert "testperson" not in text.lower()
    assert ACCOUNT not in text
    assert "This sentence must survive redaction." in text  # did not over-redact
    assert not doc.get_toc()
    assert not doc.embfile_count()
    assert not any(v for k, v in doc.metadata.items() if v and k not in ("format", "encryption"))
    assert not doc.get_xml_metadata()
    assert all(p.first_annot is None and p.first_widget is None for p in doc)
    assert all(not p.get_links() for p in doc)

    raw = all_raw_bytes(out)
    for needle in (b"testperson", b"4417-1234", b"jonathan.testperson", b"t\x00e\x00s\x00t\x00p"):
        assert needle not in raw


def char_x(doc, page, ch, below=105):
    for b in doc[page].get_text("rawdict")["blocks"]:
        for line in b.get("lines", []):
            for sp in line["spans"]:
                for c in sp["chars"]:
                    if c["c"] == ch and c["bbox"][1] < below:
                        return c["bbox"][0]


def test_text_after_redaction_does_not_move(tricky, tmp_path):
    # PyMuPDF 2.1 shifted the rest of the line left after a redaction,
    # sliding surviving words under the black box. Pinned < 2 for this.
    out = tmp_path / "out.pdf"
    _, report = run_apply(tricky, out, [SECRET])
    assert report["ok"], report["findings"]
    before = char_x(pymupdf.open(tricky), 0, "b")  # the "b" in "being"
    after = char_x(pymupdf.open(out), 0, "b")
    assert abs(before - after) < 0.05


def test_manual_box_blanks_image_pixels(tricky, tmp_path):
    out = tmp_path / "out.pdf"
    box = {"page": 0, "rect": [300, 250, 400, 350]}
    _, report = run_apply(tricky, out, [], extra=[box])
    assert report["ok"], report["findings"]
    doc = pymupdf.open(out)
    pix = doc[0].get_pixmap(clip=pymupdf.Rect(320, 270, 380, 330))
    reds = sum(1 for i in range(0, len(pix.samples), pix.n)
               if pix.samples[i] > 150 and pix.samples[i + 1] < 80)
    assert reds == 0
    # And no red pixels survive in any embedded image either.
    for xref, *_ in doc[0].get_images(full=True):
        img = pymupdf.Pixmap(doc, xref)
        s = img.samples
        assert not any(s[i] == 200 and s[i + 1] == 30 for i in range(0, len(s), img.n))


def test_manual_box_removes_text_under_it(tricky, tmp_path):
    out = tmp_path / "out.pdf"
    s = eng.Session()
    s.cmd_open(str(tricky))
    rect = s.cmd_search(["This sentence must survive"])["hits"][0]["rect"]
    _, report = run_apply(tricky, out, [], extra=[{"page": 1, "rect": rect}])
    assert report["ok"], report["findings"]
    doc = pymupdf.open(out)
    assert "This sentence" not in doc[1].get_text()
    assert "This sentence" in doc[0].get_text()


def test_verify_flags_unredacted_term(tricky, tmp_path):
    out = tmp_path / "out.pdf"
    _, report = run_apply(tricky, out, [ACCOUNT])  # redact only the account...
    report2 = eng.verify(str(out), [SECRET], [])  # ...then check for the name
    assert report["ok"]
    assert not report2["ok"]


def test_refuses_to_overwrite_original(tricky):
    s = eng.Session()
    s.cmd_open(str(tricky))
    with pytest.raises(ValueError):
        s.cmd_apply(str(tricky), [], [])


def test_rotated_page_coordinates_line_up(tmp_path):
    src = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), f"Grantor {SECRET}", fontsize=11)
    page.set_rotation(90)
    doc.save(src)
    out = tmp_path / "out.pdf"
    _, report = run_apply(src, out, [SECRET])
    assert report["ok"], report["findings"]
    assert "Grantor" in pymupdf.open(out)[0].get_text()


def test_partial_match_removes_whole_word(tmp_path):
    src = tmp_path / "email.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "To: adbarclay92@gmail.com today", fontsize=11)
    doc.save(src)
    out = tmp_path / "out.pdf"
    _, report = run_apply(src, out, ["Barclay"])
    assert report["ok"], report["findings"]
    text = pymupdf.open(out)[0].get_text()
    assert "adbarclay" not in text and "gmail" not in text and "92" not in text
    assert "To:" in text and "today" in text


def shapes_only_pdf(path: Path) -> None:
    """Like "Microsoft Print to PDF": letters are small filled shapes, no text at all."""
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(10):
        page.draw_rect(pymupdf.Rect(72 + i * 8, 100, 78 + i * 8, 108), color=None, fill=(0, 0, 0))
    doc.save(path)


def test_shape_only_page_is_reported_unreadable(tmp_path):
    src = tmp_path / "printed.pdf"
    shapes_only_pdf(src)
    s = eng.Session()
    assert s.cmd_open(str(src))["unreadable_pages"] == [1]
    report = s.cmd_apply(str(tmp_path / "out.pdf"), [], ["anything"])
    # Never silently "verified": either OCR-checked or reported as unchecked.
    assert (report["ocr_pages"] if eng.ocr_available() else report["unreadable_pages"]) == [1]


def test_manual_box_snaps_to_half_covered_letter_shapes(tmp_path):
    src = tmp_path / "printed.pdf"
    shapes_only_pdf(src)
    out = tmp_path / "out.pdf"
    # Box covers letters 2-4 fully and more than half of letter 5 (x 104-110), not letter 6 (x 112).
    s = eng.Session()
    s.cmd_open(str(src))
    s.cmd_apply(str(out), [{"page": 0, "rect": [86, 98, 108, 110]}], [])
    left = [d["rect"] for d in pymupdf.open(out)[0].get_drawings() if d.get("fill") == (0.0, 0.0, 0.0)]
    small = [r for r in left if r.width < 10]  # letter shapes, not the black redaction fill
    xs = sorted(round(r.x0) for r in small)
    assert 104 not in xs  # the half-covered letter was removed, not hidden
    assert 112 in xs and 72 in xs  # neighbours survive


def test_ocr_find_tolerates_lookalike_letters_and_covers_whole_words():
    R = pymupdf.Rect
    lines = [[("To:", R(0, 0, 10, 5)), ("adbarclay92@gmaiI.com", R(12, 0, 60, 5)), ("today", R(62, 0, 80, 5))]]
    hits = eng.ocr_find(lines, "gmail.com")
    assert len(hits) == 1 and hits[0].x0 < 12 and hits[0].x1 > 60  # whole word, padded
    assert len(eng.ocr_find(lines, "BARCLAY")) == 1
    assert len(eng.ocr_find(lines, "to: adbarclay")) == 1  # spans two words
    assert not eng.ocr_find(lines, "smith")


def scanned_pdf(path: Path) -> None:
    """A page that is only a picture of text, like a scan."""
    text = pymupdf.open()
    page = text.new_page()
    page.insert_text((72, 100), f"Grantor: {SECRET}", fontsize=14)
    page.insert_text((72, 140), "Recorded in the county records.", fontsize=14)
    img = page.get_pixmap(dpi=200)
    doc = pymupdf.open()
    doc.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=img)
    doc.save(path)


@pytest.mark.skipif(not eng.ocr_available(), reason="Windows OCR not available")
def test_ocr_finds_and_removes_name_on_scanned_page(tmp_path):
    src = tmp_path / "scan.pdf"
    scanned_pdf(src)
    out = tmp_path / "out.pdf"
    hits, report = run_apply(src, out, ["Testperson"])
    assert hits and all(h.get("ocr") for h in hits)
    assert report["ok"], report["findings"]
    assert report["ocr_pages"] == [1]
    lines = eng.ocr_lines(pymupdf.open(out)[0])
    words = " ".join(w for line in lines for w, _ in line).lower()
    assert "testperson" not in words
    assert "recorded" in words  # the rest survives


@pytest.mark.skipif(not eng.ocr_available(), reason="Windows OCR not available")
def test_verify_catches_name_left_on_scanned_page(tmp_path):
    src = tmp_path / "scan.pdf"
    scanned_pdf(src)
    report = eng.verify(str(src), ["Testperson"], [])
    assert not report["ok"]


def test_folder_mode_lists_searches_and_saves_each_file(tmp_path):
    folder = tmp_path / "estate"
    (folder / "emails").mkdir(parents=True)
    make_tricky_pdf(folder / "will.pdf")
    clean = pymupdf.open()
    clean.new_page().insert_text((72, 100), "Nothing to hide here.")
    clean.set_metadata({"author": SECRET})  # name only in metadata
    clean.save(folder / "emails" / "note.pdf")
    (folder / "old-redacted.pdf").write_bytes((folder / "will.pdf").read_bytes())  # earlier output: skipped

    s = eng.Session()
    paths = s.cmd_list_pdfs(str(folder))["paths"]
    assert [Path(p).name for p in paths] == ["will.pdf", "note.pdf"]
    out_dir = tmp_path / "out"
    for p in paths:
        s.cmd_open(p)
    for p in paths:
        hits = s.cmd_search([SECRET], path=p)["hits"]
        out = s.cmd_out_path(p, str(folder), str(out_dir))["out_path"]
        report = s.cmd_apply(out, hits, [SECRET], path=p)
        assert report["ok"], (p, report["findings"])
    assert (out_dir / "will-redacted.pdf").exists()
    note = pymupdf.open(out_dir / "emails" / "note-redacted.pdf")
    assert not note.metadata["author"]  # a file with no matches still gets scrubbed
    s.cmd_close_all()
    with pytest.raises(ValueError):
        s.cmd_search([SECRET])


def test_link_kept_alive_by_accessibility_tags_is_removed(tmp_path):
    # Chrome's "Save as PDF" tags every link in the structure tree. Removing the
    # link from the page left the annotation (and its mailto: address) in the file.
    src = tmp_path / "tagged.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Write to me anytime.", fontsize=11)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 90, 200, 105),
                      "uri": "mailto:jonathan.testperson@example.com"})
    doc.save(src)
    doc = pymupdf.open(src)
    annot = doc[0].get_links()[0]["xref"]
    tree, elem = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(elem, f"<< /Type /StructElem /S /Link /P {tree} 0 R /Alt (Email Jonathan Testperson) "
                            f"/K << /Type /OBJR /Obj {annot} 0 R >> >>")
    doc.update_object(tree, f"<< /Type /StructTreeRoot /K [{elem} 0 R] >>")
    doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{tree} 0 R")
    doc.xref_set_key(doc.pdf_catalog(), "MarkInfo", "<< /Marked true >>")
    doc.save(tmp_path / "tagged2.pdf")

    out = tmp_path / "out.pdf"
    _, report = run_apply(tmp_path / "tagged2.pdf", out, ["Testperson"])
    assert report["ok"], report["findings"]
    assert b"testperson" not in all_raw_bytes(out)
    assert "Write to me anytime." in pymupdf.open(out)[0].get_text()


def test_verify_names_where_a_leftover_hides(tmp_path):
    src = tmp_path / "linked.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 90, 200, 105),
                      "uri": "mailto:jonathan.testperson@example.com"})
    doc.save(src)
    findings = eng.verify(str(src), ["Testperson"], [])["findings"]
    assert any("a link address" in f for f in findings), findings


def test_skipped_matches_are_not_failures_but_hidden_copies_are(tmp_path):
    src = tmp_path / "will.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Will Testperson signs this Last Will and Testament.", fontsize=11)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 90, 120, 105), "uri": "https://example.com/will"})
    doc.save(src)
    s = eng.Session()
    s.cmd_open(str(src))
    hits = s.cmd_search(["Will"])["hits"]
    assert len(hits) == 2
    first, second = sorted(hits, key=lambda h: h["rect"][0])
    out = tmp_path / "out.pdf"
    report = s.cmd_apply(str(out), [first], ["Will"], skipped=[{"page": 0, "term": "Will"}])
    assert report["ok"], report["findings"]  # "Last Will" was kept on purpose; the link was removed
    assert "Last Will and Testament" in pymupdf.open(out)[0].get_text()
    # Without declaring the skip, the kept word is reported.
    assert not eng.verify(str(out), ["Will"], [])["ok"]
    # A kept term hidden outside the page is still caught.
    leaky = pymupdf.open(out)
    leaky.set_metadata({"title": "Will"})
    leaky.save(tmp_path / "leaky.pdf")
    r = eng.verify(str(tmp_path / "leaky.pdf"), ["Will"], [], [{"page": 0, "term": "Will"}])
    assert not r["ok"]


def test_term_set_round_trip(tmp_path):
    s = eng.Session()
    p = tmp_path / "family.txt"
    s.cmd_save_terms(str(p), [SECRET, "  4417-1234 ", ""])
    p.write_text(p.read_text(encoding="utf-8") + "# a comment\n\nBarclay\n", encoding="utf-8")
    assert s.cmd_load_terms(str(p))["terms"] == [SECRET, "4417-1234", "Barclay"]


def test_protocol_round_trip(tricky, tmp_path):
    script = Path(__file__).resolve().parents[1] / "rasa_engine.py"
    out = tmp_path / "out.pdf"
    reqs = [
        {"id": 1, "cmd": "open", "args": {"path": str(tricky)}},
        {"id": 2, "cmd": "search", "args": {"terms": [SECRET]}},
        {"id": 3, "cmd": "nope", "args": {}},
    ]
    p = subprocess.run([sys.executable, str(script)], input="\n".join(json.dumps(r) for r in reqs) + "\n",
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    replies = [json.loads(line) for line in p.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["ok"] and len(replies[0]["result"]["pages"]) == 3
    assert replies[1]["ok"] and replies[1]["result"]["hits"]
    assert not replies[2]["ok"]
