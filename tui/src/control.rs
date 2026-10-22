// SPDX-FileCopyrightText: 2026 Mateusz Okulanis
// SPDX-License-Identifier: GPL-3.0-or-later

//! Writing to a run's control inbox.
//!
//! The viewer does not reach into the harness to stop a run: it appends a line
//! to `control.jsonl`, exactly as `nih cancel` and an agent over MCP do, and
//! the run turns that request into an event when it acts on it. One mechanism,
//! three drivers, one audit trail.
//!
//! The line's shape is the `ControlMessage` schema under `schemas/`, minus the
//! identity, which the inbox assigns when it reads the line. The timestamp is
//! epoch seconds, which the harness reads as an instant in UTC. Between the
//! two, writing a command needs nothing here but a JSON serialiser.

use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::json;

/// Name of the inbox inside a run's directory.
pub(crate) const CONTROL_FILE: &str = "control.jsonl";

/// Return the inbox path for a run directory.
#[must_use]
pub(crate) fn inbox_path(run_dir: &Path) -> PathBuf {
    run_dir.join(CONTROL_FILE)
}

/// Seconds since the epoch, as the harness reads timestamps.
fn now_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or_default()
}

/// Ask a run to stop at its next step boundary.
///
/// # Errors
///
/// Returns the I/O error if the inbox cannot be appended to.
pub(crate) fn submit_cancel(run_dir: &Path, run_id: &str, reason: &str) -> io::Result<()> {
    let line = json!({
        "ts": now_epoch(),
        "command": {
            "kind": "cancel_run",
            "source": "tui",
            "run_id": run_id,
            "reason": reason,
        },
    });
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(inbox_path(run_dir))?;
    writeln!(file, "{line}")?;
    file.flush()
}

#[cfg(test)]
#[allow(
    clippy::expect_used,
    reason = "a test that cannot set its fixture up should fail loudly"
)]
mod tests {
    use super::{inbox_path, submit_cancel};
    use std::fs;

    #[test]
    fn a_cancel_is_one_line_the_harness_can_read() {
        let dir = std::env::temp_dir().join(format!("nih-tui-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("the temporary directory is writable");
        submit_cancel(&dir, "20261022-100000-abcdef", "esc").expect("the inbox accepts the line");

        let body = fs::read_to_string(inbox_path(&dir)).expect("the inbox was written");
        let parsed: serde_json::Value =
            serde_json::from_str(body.lines().next().expect("one line")).expect("valid json");
        assert_eq!(parsed["command"]["kind"], "cancel_run");
        assert_eq!(parsed["command"]["source"], "tui");
        assert_eq!(parsed["command"]["run_id"], "20261022-100000-abcdef");
        assert_eq!(parsed["command"]["reason"], "esc");
        assert!(parsed["ts"].as_f64().unwrap_or_default() > 0.0);
        assert!(
            parsed.get("id").is_none(),
            "the inbox assigns the identity, so the viewer does not hash anything"
        );
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn appending_twice_leaves_two_lines() {
        let dir = std::env::temp_dir().join(format!("nih-tui-two-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("the temporary directory is writable");
        submit_cancel(&dir, "r", "first").expect("first line");
        submit_cancel(&dir, "r", "second").expect("second line");
        let body = fs::read_to_string(inbox_path(&dir)).expect("the inbox was written");
        assert_eq!(body.lines().count(), 2);
        fs::remove_dir_all(&dir).ok();
    }
}
