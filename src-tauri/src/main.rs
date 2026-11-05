// No console window in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! The window is a thin shell. All PDF work happens in the engine process
//! (engine/rasa_engine.py), which this file starts and talks to over
//! one-JSON-object-per-line pipes. Nothing touches the network.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::Mutex;

use serde_json::Value;

struct Engine {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Drop for Engine {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

#[derive(Default)]
struct EngineState(Mutex<Option<Engine>>);

/// Dev builds run the engine from source with uv; release builds run the
/// frozen sidecar that Tauri installs next to the app executable.
#[cfg(debug_assertions)]
fn engine_command() -> Result<Command, String> {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../engine");
    let mut cmd = Command::new("uv");
    cmd.arg("run").arg("--project").arg(&dir).arg("python").arg(dir.join("rasa_engine.py"));
    Ok(cmd)
}

#[cfg(not(debug_assertions))]
fn engine_command() -> Result<Command, String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let dir = exe.parent().ok_or("cannot locate the app folder")?;
    let name = if cfg!(windows) { "rasa-engine.exe" } else { "rasa-engine" };
    Ok(Command::new(dir.join(name)))
}

fn spawn_engine() -> Result<Engine, String> {
    let mut cmd = engine_command()?;
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd.spawn().map_err(|e| format!("Could not start the redaction engine: {e}"))?;
    let stdin = child.stdin.take().ok_or("engine stdin unavailable")?;
    let stdout = BufReader::new(child.stdout.take().ok_or("engine stdout unavailable")?);
    Ok(Engine { child, stdin, stdout })
}

fn round_trip(engine: &mut Engine, line: &str) -> std::io::Result<String> {
    writeln!(engine.stdin, "{line}")?;
    engine.stdin.flush()?;
    let mut reply = String::new();
    if engine.stdout.read_line(&mut reply)? == 0 {
        return Err(std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "the engine stopped"));
    }
    Ok(reply)
}

/// Forward one request to the engine and return its result (or its error message).
#[tauri::command]
async fn engine(state: tauri::State<'_, EngineState>, request: Value) -> Result<Value, String> {
    let mut slot = state.0.lock().map_err(|_| "engine lock poisoned".to_string())?;
    if slot.is_none() {
        *slot = Some(spawn_engine()?);
    }
    let line = serde_json::to_string(&request).map_err(|e| e.to_string())?;
    let reply = match round_trip(slot.as_mut().unwrap(), &line) {
        Ok(r) => r,
        Err(e) => {
            *slot = None; // restart on next request
            return Err(format!("Redaction engine failed: {e}"));
        }
    };
    let v: Value = serde_json::from_str(&reply).map_err(|e| format!("Bad engine reply: {e}"))?;
    if v["ok"] == Value::Bool(true) {
        Ok(v["result"].clone())
    } else {
        Err(v["error"].as_str().unwrap_or("unknown engine error").to_string())
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(EngineState::default())
        .invoke_handler(tauri::generate_handler![engine])
        .run(tauri::generate_context!())
        .expect("error while running rasa");
}
