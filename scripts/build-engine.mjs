// Freeze the Python engine into one executable and place it where Tauri's
// externalBin expects it: src-tauri/binaries/rasa-engine-<target-triple>[.exe]
import { execSync } from "node:child_process";
import { copyFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const root = join(import.meta.dirname, "..");
const engine = join(root, "engine");
const triple = execSync("rustc -vV").toString().match(/host: (\S+)/)[1];
const ext = process.platform === "win32" ? ".exe" : "";

execSync(
  "uv run --group build pyinstaller --onefile --console --clean --noconfirm --name rasa-engine " +
    "--collect-submodules winrt --exclude-module tkinter --exclude-module pymupdf4llm --exclude-module pymupdf.layout rasa_engine.py",
  { cwd: engine, stdio: "inherit" },
);

const outDir = join(root, "src-tauri", "binaries");
mkdirSync(outDir, { recursive: true });
copyFileSync(join(engine, "dist", `rasa-engine${ext}`), join(outDir, `rasa-engine-${triple}${ext}`));
console.log(`engine -> src-tauri/binaries/rasa-engine-${triple}${ext}`);
