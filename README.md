# rasa

*Tabula rasa: a tablet scraped clean.*

A desktop app for **permanent** PDF redaction that runs entirely on your machine.
Type a name or number and every instance in the document is removed. Drag boxes
over anything else (signatures, pictures, logos). The original is never modified;
rasa writes a new copy and then checks that copy for leftovers.

## What "permanent" means here

Drawing a black rectangle over text does nothing: the words are still in the file
and anyone can copy them out. rasa deletes the content itself:

- **Text** under each box is deleted from the page's content stream.
- **Image pixels** under each box are blanked inside the image data.
- **Vector graphics** fully under a box are removed.
- **Everything outside the page** that could hold the same words is stripped:
  document metadata and XMP, bookmarks, links (including `mailto:`), annotations
  and comments, form fields, attachments, JavaScript, thumbnails, and invisible
  text layers. Annotations and form fields are first flattened into the page, so
  their visible text is searched and redacted like anything else.
- The file is **rewritten from scratch** with unused objects garbage-collected,
  so earlier revisions of the document are not carried along.

After saving, rasa re-opens the new file and checks for each search term in the
extracted text, in the raw bytes of every PDF object and decompressed stream, and
under every box, plus that metadata, bookmarks, attachments and annotations are
empty. If anything is found, it tells you not to share the file.

### Limits

- **Box size reveals text length.** The rest of the line stays where it was, so the
  width of a black box shows roughly how long the removed text was. With context,
  a short name can sometimes be guessed. Redact surrounding words too if that matters.
- **Search only sees real text.** Scanned pages and text inside pictures are not
  searchable. Cover them with manual boxes.
- **Search matches what you type.** "Smith" will not find "S M I T H" or "Smyth".
  Add every variant you need.
- **Password-protected PDFs** must be unlocked first.

## Use

Download the installer from Releases, or build it yourself:

```sh
npm install
npm run build        # freezes the engine, then builds the installer
```

Development:

```sh
npm run dev          # runs the engine from source with uv
npm test             # engine tests
```

Requires Rust, Node 20+, and [uv](https://docs.astral.sh/uv/).

## How it is built

- `ui/`: plain HTML/JS interface (no framework, no network access; the CSP allows only local IPC).
- `src-tauri/`: the Tauri window. It starts the engine and passes JSON lines to it.
- `engine/`: the Python engine using [PyMuPDF](https://pymupdf.readthedocs.io/) (MuPDF's
  redaction). Frozen into a single executable with PyInstaller for release builds.

PyMuPDF is pinned below 2.0. PyMuPDF 2.1 shifts the rest of a line left after a
redaction, which can slide surviving words under the black box. There is a test for this.

## License

AGPL-3.0, as required by PyMuPDF.
