use serde::Serialize;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager, State};

/// Shared, process-wide runtime state for the desktop shell.
struct AppState {
    bok: Mutex<Option<Child>>,
}

#[derive(Serialize, Clone)]
struct ServiceStatus {
    name: String,
    port: u16,
    up: bool,
}

#[derive(Serialize, Clone)]
struct HealthReport {
    app_data_dir: String,
    services: Vec<ServiceStatus>,
}

const PORTS: &[(&str, u16)] = &[
    ("control-plane", 8000),
    ("web", 3000),
    ("asr", 8787),
    ("tts", 8788),
    ("llm", 1235),
    ("b-line", 8790),
    ("livekit", 7880),
];

fn is_up(port: u16) -> bool {
    TcpStream::connect_timeout(&format!("127.0.0.1:{}", port).parse().unwrap_or_else(|_| "127.0.0.1:0".parse().unwrap()), Duration::from_millis(600)).is_ok()
}

/// Resolve the repository root that contains `tools/bok.py`.
///
/// Order:
///   1. BOK_ROOT env var (CI / forked dev)
///   2. Tauri resource dir (packaged app, resources assigned to bundle)
///   3. `desktop/src-tauri` parent.paren two levels up in a dev clone
fn resolve_root(app: &AppHandle) -> PathBuf {
    if let Ok(v) = std::env::var("BOK_ROOT") {
        let p = PathBuf::from(v);
        if p.join("tools/bok.py").exists() {
            return p;
        }
    }
    if let Ok(res) = app.path().resource_dir() {
        let p = res.join("desktop");
        if p.join("tools/bok.py").exists() {
            return p.parent().map(|x| x.to_path_buf()).unwrap_or(p);
        }
        if res.join("tools/bok.py").exists() {
            return res;
        }
    }
    // Dev build: CARGO_MANIFEST_DIR is `desktop/src-tauri`; the repo root is
    // two levels up. This is compile-time so it also works when the binary is
    // run from `cargo run`/`target/debug`.
    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let here = PathBuf::from(manifest);
        for cand in [here.clone(), here.join(".."), here.join("../.."), here.join("../../..")] {
            if cand.join("tools/bok.py").exists() {
                return cand;
            }
        }
    }
    // Dev fallback: `desktop/src-tauri` -> repo root.
    let here = std::env::current_exe()
        .map(|exe| exe.parent().map(|x| x.to_path_buf()).unwrap_or_default())
        .unwrap_or_default();
    for cand in [here.clone(), here.join(".."), here.join("../.."), here.join("../../..")] {
        if cand.join("tools/bok.py").exists() {
            return cand;
        }
    }
    PathBuf::from(".")
}

fn python() -> String {
    if cfg!(target_os = "windows") {
        "python".to_string()
    } else {
        "python3".to_string()
    }
}

/// Prefer a bundled runtime venv (packaged app), then the repo venv, then the
/// system python. This lets a self-contained build run without any system
/// Python installed.
fn bundled_python(root: &PathBuf) -> String {
    let candidates: Vec<PathBuf> = if cfg!(target_os = "windows") {
        vec![
            root.join("runtime/.venv/Scripts/python.exe"),
            root.join(".venv312/Scripts/python.exe"),
            root.join(".venv/Scripts/python.exe"),
        ]
    } else {
        vec![
            root.join("runtime/.venv/bin/python"),
            root.join(".venv312/bin/python"),
            root.join(".venv/bin/python"),
        ]
    };
    for c in candidates {
        if c.exists() {
            return c.to_string_lossy().to_string();
        }
    }
    python()
}

/// Spawn `python tools/bok.py <subcommand>` detached, streaming output to
/// `app-data/logs/bok-<cmd>.log`. Returns the child pid.
fn spawn_bok(_app: &AppHandle, root: &PathBuf, cmd_name: &str) -> Result<Child, String> {
    let bok = root.join("tools/bok.py");
    if !bok.exists() {
        return Err(format!("bok launcher missing: {}", bok.display()));
    }
    let use_py = bundled_python(root);
    let log_dir = app_data_dir().join("logs");
    std::fs::create_dir_all(&log_dir).map_err(|e| e.to_string())?;
    let log_path = log_dir.join(format!("bok-{}.log", cmd_name));
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| e.to_string())?;
    let child = Command::new(&use_py)
        .arg(&bok)
        .arg(cmd_name)
        .current_dir(root)
        .env("BOK_ROOT", root)
        .env("BOK_PACKAGED", "1")
        .stdout(Stdio::from(log.try_clone().map_err(|e| e.to_string())?))
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(child)
}

fn app_data_dir() -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| dirs::data_local_dir().unwrap_or_default());
        base.join("BokVoice")
    }
    #[cfg(not(target_os = "windows"))]
    {
        let base = dirs::data_dir().unwrap_or_default();
        base.join("BokVoice")
    }
}

fn report() -> HealthReport {
    HealthReport {
        app_data_dir: app_data_dir().display().to_string(),
        services: PORTS
            .iter()
            .map(|(name, port)| ServiceStatus {
                name: name.to_string(),
                port: *port,
                up: is_up(*port),
            })
            .collect(),
    }
}

#[tauri::command]
fn health() -> HealthReport {
    report()
}

#[tauri::command]
fn start(app: AppHandle, state: State<AppState>) -> Result<String, String> {
    let mut child = state.bok.lock().map_err(|_| "state lock")?;
    if child.is_some() {
        return Ok("already-starting".to_string());
    }
    let root = resolve_root(&app);
    *child = Some(spawn_bok(&app, &root, "serve")?);
    Ok("started".to_string())
}

#[tauri::command]
fn stop(app: AppHandle, state: State<AppState>) -> Result<String, String> {
    let mut child = state.bok.lock().map_err(|_| "state lock")?;
    match child.take() {
        Some(mut c) => {
            let _ = c.kill();
            let _ = c.wait();
            Ok("stopped".to_string())
        }
        None => {
            let _ = spawn_bok(&app, &resolve_root(&app), "down");
            Ok("stopped".to_string())
        }
    }
}

#[tauri::command]
fn open_logs(_app: AppHandle) -> Result<String, String> {
    let dir = app_data_dir().join("logs");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    #[cfg(target_os = "macos")]
    Command::new("open").arg(&dir).spawn().map_err(|e| e.to_string())?;
    #[cfg(target_os = "windows")]
    Command::new("explorer").arg(&dir).spawn().map_err(|e| e.to_string())?;
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let _ = &dir;
    Ok(dir.display().to_string())
}

#[tauri::command]
fn manifest(app: AppHandle) -> Result<String, String> {
    let root = resolve_root(&app);
    let out = Command::new(python())
        .arg(root.join("tools/bok.py"))
        .arg("manifest")
        .current_dir(&root)
        .output()
        .map_err(|e| e.to_string())?;
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

fn run_bok_json(app: &AppHandle, args: &[&str]) -> Result<String, String> {
    let root = resolve_root(app);
    let use_py = bundled_python(&root);
    let mut cmd = Command::new(&use_py);
    cmd.arg(root.join("tools/bok.py"))
        .args(args)
        .current_dir(&root)
        .env("BOK_ROOT", &root)
        .env("BOK_PACKAGED", "0");
    let out = cmd.output().map_err(|e| e.to_string())?;
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

#[tauri::command]
fn setup_status(app: AppHandle) -> Result<String, String> {
    run_bok_json(&app, &["setup", "status"])
}

#[tauri::command]
fn setup_download(app: AppHandle) -> Result<String, String> {
    // download can be slow; run as a detached child and return quickly. The
    // front-end polls setup_status to reflect progress.
    let root = resolve_root(&app);
    let use_py = bundled_python(&root);
    let log_dir = app_data_dir().join("logs");
    std::fs::create_dir_all(&log_dir).map_err(|e| e.to_string())?;
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("bok-setup.log"))
        .map_err(|e| e.to_string())?;
    Command::new(&use_py)
        .arg(root.join("tools/bok.py"))
        .args(["setup", "download"])
        .current_dir(&root)
        .env("BOK_ROOT", &root)
        .env("BOK_PACKAGED", "0")
        .stdout(Stdio::from(log.try_clone().map_err(|e| e.to_string())?))
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok("started".to_string())
}

pub fn run() {
    tauri::Builder::default()
        .manage(AppState { bok: Mutex::new(None) })
        .invoke_handler(tauri::generate_handler![
            health,
            start,
            stop,
            open_logs,
            manifest,
            setup_status,
            setup_download
        ])
        .setup(|app| {
            let root = resolve_root(app.handle());
            let _ = spawn_bok(app.handle(), &root, "serve");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_root_finds_repo_via_cargo_manifest_dir() {
        // resolve_root prefers BOK_ROOT, then resource_dir, then CARGO_MANIFEST_DIR.
        // In a unit test we cannot call resolve_root (needs AppHandle), but we can
        // assert the compile-time manifest dir points at the expected repo layout.
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        assert!(manifest.file_name().map(|s| s.to_string_lossy().to_string()).is_some());
        // desktop/src-tauri -> ../.. is the repo root with tools/bok.py.
        let repo = manifest.join("../..");
        assert!(repo.join("tools/bok.py").exists(), "expected repo tools/bok.py");
        assert!(repo.join("packages/observability").exists(), "expected observability package");
    }

    #[test]
    fn is_up_returns_bool_on_known_port() {
        // A definitely-closed port must be DOWN, and a definitely-open one (the
        // test process's binding) should be UP. We use port 1 (never reachable)
        // to assert the false path without leaking a real socket.
        assert!(!is_up(1));
    }
}
